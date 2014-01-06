#!/usr/bin/env python
# encoding: utf-8

"""
pipe.py

Implements the HCPrepWorkflow and related methods, and handles command line
calls.

Copyright (c) 2013 Ben Acland
"""

import os
import sys
import getopt

import nipype.interfaces.io as nio           # Data i/o
import nipype.interfaces.utility as util     # utility
import nipype.pipeline.engine as pe          # pypeline engine

from duke_siemens.util_dicom_siemens import read as read_siemens_shadow
from config import *
from interfaces import *
from util import *

help_message = """
Implements the HCPrepWorkflow, provides command line tools to build and work
on workflow configuration files, and for launching the pipeline using a
specific config. You can either import HCPrepWorkflow, then configure and run
in from your own code, or use the built-in command line tools to launch the
workflow on your data.

Commands
--------
-h, --help
    Prints out this message.
-i, --init
    Creates a new config file in your current directory.
-u, --update
    Reruns part of the config file setup script on any config file in your current directory.
-g, --graph
    Draws a graph of the nipype workflow to the current directory.
-r, --run
    Runs the workflow using any config file in the current directory.

Parameters
----------
-c, --config (path)
    The config file to use.
-s (comma separated subject numbers)
    The list of subjects who you'd like to put through the workflow. Overrides
    setting in the config file.
-n (integer)
    The number of threads you would like to use. Higher numbers are faster to
    a point, but depending on how large your data is (chances are, it is quite
    large), you may well want to limit yourself to something below 8 for
    starters if you're working on a large server. I haven't added command line
    support for clusters yet, but it'll come soon. Default is 1.
-o (path)
    The directory to put preprocessed data in. Default is current directory.
-v 
    Verbose. At this point, just makes -g print a graph that expands iterables.

"""

# TODO: validate these guesses... user may need to look at an image and flip an option if necessary
# Positive fieldmaps: RL, AP
# Negative fieldmaps: LR, PA

# TODO: update to account for the scalar -> list changes in HCP code (prefs, I think...)

# TODO: use the following values to select the correct template resolution?
# (0028, 0030) Pixel Spacing                       DS: ['0.80000001192093', '0.80000001192093']
# (0018, 0050) Slice Thickness                     DS: '0.79999995231628'

class HCPrepWorkflow(pe.Workflow):
    def __init__(self, config=None, *args, **kwargs):
        super(HCPrepWorkflow, self).__init__(*args, **kwargs)
        self.hc_config = config

    @property
    def hc_config(self):
        return self._hc_config
    @hc_config.setter
    def hc_config(self, value):
        self._hc_config = value
        if self._hc_config:
            self.update_nodes_from_config()

    def get_conf(self, section, option):
        if not self.hc_config:
            return None
        return self.hc_config.get(section,{}).get(option, None)

    def update_nodes_from_config(self):
        # subjects node
        subs = self.get_conf("general","subjects")
        if subs:
            self.subjects_node.iterables = ("subject", subs)
        # dcm grabber
        sub_dir = self.get_conf("general","subject_dir")
        dcm_temp = self.get_conf("general","dicom_template")
        if sub_dir:
            self.dicom_grabber.inputs.base_directory = sub_dir
        if dcm_temp:
            self.dicom_grabber.inputs.field_template = {"dicom": dcm_temp}
        # nifti wrangler
        series_map = self.hc_config.get("series", {})
        if series_map:
            self.nii_wrangler.inputs.series_map = series_map
        # set template and config values (names are also input names on some nodes)
        temps = self.hc_config.get("templates", {})
        c_files = self.hc_config.get("config_files", {})
        for n in [self.hc_pre_fs, self.hc_fs, self.hc_post_fs, self.hc_volume, self.hc_surface]:
            apply_dict_to_obj(temps, n.inputs, skip_names=["templates_dir"])
            apply_dict_to_obj(c_files, n.inputs)
        # set the commands for each of the freesurfer steps... this stinks. improve when you can.
        (self.hc_pre_fs.inputs.full_command,
            self.hc_fs.inputs.full_command,
            self.hc_post_fs.inputs.full_command,
            self.hc_volume.inputs.full_command,
            self.hc_surface.inputs.full_command) = get_hcp_commands_for_config(self.hc_config)
        # set the environ on each of the hc nodes
        envs = get_hcp_env_for_config(self.hc_config)
        self.hc_pre_fs.inputs.environ = envs
        self.hc_fs.inputs.environ = envs
        self.hc_post_fs.inputs.environ = envs
        self.hc_volume.inputs.environ = envs
        self.hc_surface.inputs.environ = envs
        # any other per-step hcp config - a good place to overide un-derived values
        apply_dict_to_obj(self.hc_config.get("nifti_wrangler", {}), self.nii_wrangler.inputs)
        apply_dict_to_obj(self.hc_config.get("pre_freesurfer", {}), self.hc_pre_fs.inputs)
        apply_dict_to_obj(self.hc_config.get("freesurfer", {}), self.hc_fs.inputs)
        apply_dict_to_obj(self.hc_config.get("post_freesurfer", {}), self.hc_post_fs.inputs)
        apply_dict_to_obj(self.hc_config.get("volume_processing", {}), self.hc_volume.inputs)
        apply_dict_to_obj(self.hc_config.get("surface_processing", {}), self.hc_surface.inputs)

    def run(self, *args, **kwargs):
        self.connect_nodes()
        super(HCPrepWorkflow, self).run(*args, **kwargs)

    def write_graph(self, *args, **kwargs):
        self.connect_nodes()
        super(HCPrepWorkflow, self).write_graph(*args, **kwargs)
        
    def clear_nodes(self):
        all_nodes = self._get_all_nodes()
        if all_nodes is not None:
            self.remove_nodes(all_nodes)

    def connect_nodes(self):
        # Some connections that don't change
        self.clear_nodes()
        self.connect([
            # prep steps
            (self.subjects_node, self.dicom_grabber, [("subject", "subject")]),
            (self.dicom_grabber, self.dicom_gather, [("dicom", "files")]),
            (self.dicom_gather, self.dicom_select, [("links", "inlist")]),
            (self.dicom_select, self.dicom_convert, [("out", "source_names")]),
            (self.dicom_gather, self.dicom_info, [("links", "files")]),
            (self.dicom_convert, self.nii_wrangler, [("converted_files", "nii_files")]),
            (self.dicom_info, self.nii_wrangler, [("info", "dicom_info")]),
            # pre freesurfer
            (self.subjects_node, self.hc_pre_fs, [("subject", "subject")]),
            (self.nii_wrangler, self.hc_pre_fs, [("t1_structs", "t1_files")]),
            (self.nii_wrangler, self.hc_pre_fs, [("t2_structs", "t2_files")]),
            (self.nii_wrangler, self.hc_pre_fs, [("mag_fieldmap", "fieldmap_magnitude")]),
            (self.nii_wrangler, self.hc_pre_fs, [("phase_fieldmap", "fieldmap_phase")]),
            (self.nii_wrangler, self.hc_pre_fs, [("fieldmap_te", "fieldmap_te")]),
            (self.nii_wrangler, self.hc_pre_fs, [("t1_sample_spacing", "t1_sample_spacing")]),
            (self.nii_wrangler, self.hc_pre_fs, [("t2_sample_spacing", "t2_sample_spacing")]),
            # freesurfer
            (self.hc_pre_fs, self.hc_fs, [("subject", "subject")]),
            (self.hc_pre_fs, self.hc_fs, [("subject_t1_dir", "subject_t1_dir")]),
            (self.hc_pre_fs, self.hc_fs, [("t1_acpc_dc_restore", "t1_acpc_dc_restore")]),
            (self.hc_pre_fs, self.hc_fs, [("t1_acpc_dc_restore_brain", "t1_acpc_dc_restore_brain")]),
            (self.hc_pre_fs, self.hc_fs, [("t2_acpc_dc_restore", "t2_acpc_dc_restore")]),
            # post freesurfer
            (self.hc_fs, self.hc_post_fs, [("subject", "subject")]),
            (self.hc_pre_fs, self.hc_post_fs, [("study_dir", "study_dir")]),
            # volume
            (self.hc_post_fs, self.hc_volume, [("subject", "subject")]),
            (self.hc_pre_fs, self.hc_volume, [("study_dir", "study_dir")]),
            (self.nii_wrangler, self.hc_volume, [("bold_names", "bold_name")]),
            (self.nii_wrangler, self.hc_volume, [("bolds", "bold_img")]),
            (self.nii_wrangler, self.hc_volume, [("sb_refs", "bold_scout")]),
            (self.nii_wrangler, self.hc_volume, [("neg_fieldmaps", "se_fieldmap_neg")]),
            (self.nii_wrangler, self.hc_volume, [("pos_fieldmaps", "se_fieldmap_pos")]),
            (self.nii_wrangler, self.hc_volume, [("ep_echo_spacings", "fieldmap_echo_spacing")]),
            (self.nii_wrangler, self.hc_volume, [("ep_unwarp_dirs", "unwarp_dir")]),
            (self.hc_post_fs, self.hc_volume, [("grayordinates_res", "fmri_res")]),
            # surface
            (self.hc_volume, self.hc_surface, [("subject", "subject")]),
            (self.hc_volume, self.hc_surface, [("bold_name", "bold_name")]),
            (self.hc_pre_fs, self.hc_surface, [("study_dir", "study_dir")]),
            (self.hc_post_fs, self.hc_surface, [("low_res_mesh", "low_res_mesh")]),
            (self.hc_post_fs, self.hc_surface, [("grayordinates_res", "fmri_res")]),
            (self.hc_post_fs, self.hc_surface, [("grayordinates_res", "grayordinates_res")]),
            # datasink
            (self.hc_surface, self.data_sink, [("study_dir", "preprocessed")]),
            ])

    """ self-inflating nodes """
    
    @property
    def subjects_node(self):
        if not getattr(self,"_subjects_node",None):
            self._subjects_node = pe.Node(
                    name="subs_node",
                    interface=util.IdentityInterface(
                            fields=["subject"]))
        return self._subjects_node
    @subjects_node.setter
    def subjects_node(self, val):
        self._subjects_node = val

    @property
    def dicom_grabber(self):
        if not getattr(self,"_dicom_grabber",None):
            self._dicom_grabber = pe.Node(
                    name = "dicom_source_1",
                    interface = nio.DataGrabber(
                            infields = ["subject"],
                            outfields = ["dicom"],))
            self._dicom_grabber.inputs.template = "*"
            self._dicom_grabber.inputs.template_args = {"dicom": [["subject"]]}
            self._dicom_grabber.inputs.sort_filelist = True
        return self._dicom_grabber
    @dicom_grabber.setter
    def dicom_grabber(self, val):
        self._dicom_grabber = val

    @property
    def dicom_gather(self):
        if not getattr(self,'_dicom_gather',None):
            self._dicom_gather = pe.Node(name='dicom_gather', interface=GatherFiles())
        return self._dicom_gather
    @dicom_gather.setter
    def dicom_gather(self, val):
        self._dicom_gather = val

    @property
    def dicom_convert(self):
        if not getattr(self,"_dicom_convert",None):
            self._dicom_convert = pe.Node(name="dicom_convert", interface=HCDcm2nii())
            self._dicom_convert.inputs.convert_all_pars = True
            self._dicom_convert.inputs.gzip_output = False
            self._dicom_convert.inputs.reorient = False
            self._dicom_convert.inputs.reorient_and_crop = False
            self._dicom_convert.inputs.args = "-d n -p n"
        return self._dicom_convert
    @dicom_convert.setter
    def dicom_convert(self, val):
        self._dicom_convert = val

    @property
    def dicom_select(self):
        if not getattr(self,'_dicom_select',None):
            self._dicom_select = pe.Node(name="select_dicom", interface=util.Select(index = 0))
        return self._dicom_select
    @dicom_select.setter
    def dicom_select(self, val):
        self._dicom_select = val

    @property
    def dicom_info(self):
        if not getattr(self,'_dicom_info',None):
            self._dicom_info = pe.Node(name="dicom_info", interface=DicomInfo())
        return self._dicom_info
    @dicom_info.setter
    def dicom_info(self, val):
        self._dicom_info = val

    @property
    def nii_wrangler(self):
        if not getattr(self,'_nii_wrangler',None):
            self._nii_wrangler = pe.Node(name="nii_wrangler", interface=NiiWrangler())
        return self._nii_wrangler
    @nii_wrangler.setter
    def nii_wrangler(self, val):
        self._nii_wrangler = val

    @property
    def hc_pre_fs(self):
        if not getattr(self,'_hc_pre_fs',None):
            self._hc_pre_fs = pe.Node(name="pre_freesurfer", interface=PreFS())
        return self._hc_pre_fs
    @hc_pre_fs.setter
    def hc_pre_fs(self, val):
        self._hc_pre_fs = val

    @property
    def hc_fs(self):
        if not getattr(self,'_hc_fs',None):
            self._hc_fs = pe.Node(name="freesurfer", interface=FS())
        return self._hc_fs
    @hc_fs.setter
    def hc_fs(self, val):
        self._hc_fs = val

    @property
    def hc_post_fs(self):
        if not getattr(self,'_hc_post_fs',None):
            self._hc_post_fs = pe.Node(name="post_freesurfer", interface=PostFS())
        return self._hc_post_fs
    @hc_post_fs.setter
    def hc_post_fs(self, val):
        self._hc_post_fs = val

    @property
    def hc_volume(self):
        if not getattr(self,'_hc_volume',None):
            self._hc_volume = pe.MapNode(
                    name="volume",
                    interface=VolumeProcessing(),
                    iterfield=["bold_name",
                               "bold_img",
                               "bold_scout",
                               "se_fieldmap_pos",
                               "se_fieldmap_neg",
                               "unwarp_dir",
                               "fieldmap_echo_spacing"])
        return self._hc_volume
    @hc_volume.setter
    def hc_volume(self, val):
        self._hc_volume = val

    @property
    def hc_surface(self):
        if not getattr(self,'_hc_surface',None):
            self._hc_surface = pe.MapNode(
                    name="surface",
                    interface=SurfaceProcessing(),
                    iterfield=["bold_name",
                               "subject"]) # subject, because this gets passed from another mapnode...
        return self._hc_surface
    @hc_surface.setter
    def hc_surface(self, val):
        self._hc_surface = val

    @property
    def data_sink(self):
        if not getattr(self,'_data_sink',None):
            self._data_sink = pe.Node(name="data_sink", interface=nio.DataSink())
        return self._data_sink
    @data_sink.setter
    def data_sink(self, val):
        self._data_sink = val

class Usage(Exception):
    def __init__(self, msg=help_message):
        self.msg = msg

def main(argv=None):
    import os
    if argv is None:
        argv = sys.argv
    try:
        try:
            opts, args = getopt.getopt(argv[1:], "vrhiugs:n:o:c:", ["run", "help", "init", "update", "graph", "config"])
        except getopt.error, msg:
            raise Usage(msg="\n"+str(msg))
        # option processing
        update = False
        graph = False
        subs = None
        N_PROCS = None
        run = False
        verbose = False
        c_file = None
        out_dir = os.getcwd()
        for option, value in opts:
            if option in ("-h", "--help"):
                raise Usage()
            if option in ("-i", "--init"):
                setup_conf()
                return
            if option in ("-u", "--update"):
                update = True
            if option in ("-g", "--graph"):
                graph = True
            if option in ("-r", "--run"):
                run = True
            if option in ("-c", "--config"):
                c_file = value
            if option in ("-s"):
                subs = [sub.strip() for sub in value.split(",")]
            if option in ("-v"):
                verbose = True
            if option in ('-n'):
                N_PROCS = int_or_none(value)
            if option in ('-o'):
                out_dir = value
        # select config file
        if not c_file:
            c_file = select_conf()
        # update if necessary
        if update:
            update_conf(c_file)
            return
        # make sure we're going to do something
        if not run and not graph:
            raise Usage(msg="Nothing to do...")
        # validate the config for running
        conf = get_config_dict(c_file)
        if not conf:
            raise Usage(msg="Could not parse config file.")
        if not validate_config(conf):
            raise Usage(msg="Invalid config file.")
        # build the workflow, pass it subjects if they were given in the command line
        wk = HCPrepWorkflow(name="hcp_prep_workflow", config=conf)
        if subs:
            wk.subjects_node.iterables = ("subject", subs)
        # set the output dir
        out_dir = os.path.abspath(out_dir)
        if not os.path.exists(out_dir):
            os.makedirs(out_dir)
        wk.data_sink.inputs.base_directory = out_dir
        # graph if you like
        if graph:
            g2u = "exec" if verbose else "orig"
            wk.write_graph(dotfilename="hcp_pipe_graph", graph2use=g2u)
            return
        if not run:
            return
        if N_PROCS > 0:
            print "running with %d processes" % N_PROCS
            wk.run(plugin="MultiProc", plugin_args={"n_procs" : N_PROCS, "non_daemon" : True})
        else:
            print "running single process"
            wk.run()

    except Usage, err:
        f_str = sys.argv[0].split("/")[-1] + ":"
        lfs = len(f_str)
        f_str = "%s\n%s\n%s\n" % ("-"*lfs, f_str, "-"*lfs)
        print >> sys.stderr, f_str + str(err.msg)
        print >> sys.stderr, "-------------------\nfor help use --help\n-------------------"
        return 2

if __name__ == "__main__":
    sys.exit(main())

