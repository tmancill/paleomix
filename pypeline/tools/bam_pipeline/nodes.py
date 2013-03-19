#!/usr/bin/python
#
# Copyright (c) 2012 Mikkel Schubert <MSchubert@snm.ku.dk>
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell 
# copies of the Software, and to permit persons to whom the Software is 
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER 
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE 
# SOFTWARE.
#
import os

from pypeline.node import CommandNode, MetaNode
from pypeline.atomiccmd import AtomicCmd
from pypeline.atomicset import ParallelCmds, SequentialCmds
from pypeline.atomicparams import AtomicJavaParams

from pypeline.nodes.picard import ValidateBAMNode
from pypeline.nodes.samtools import BAMIndexNode
from pypeline.common.utilities import safe_coerce_to_tuple
from pypeline.common.fileutils import swap_ext, add_postfix

import pypeline.common.versions as versions

# Number of reads to sample when running mapDamage
_MAPDAMAGE_MAX_READS = 100000


MAPDAMAGE_VERSION = versions.Requirement(call   = ("mapDamage", "--version"),
                                         search = r"(\d+)\.(\d+)\.(\d+)",
                                         pprint = "{0}.{1}.{2}",
                                         checks = versions.GE(2, 0, 0))


class MapDamageNode(CommandNode):
    def __init__(self, config, reference, input_files, output_directory, dependencies):
        cmd_cat = _concatenate_input_bams(config, input_files)

        cmd_map = AtomicCmd(["mapDamage", "--no-stats",
                            "-n", _MAPDAMAGE_MAX_READS,
                             "-i", "-",
                             "-d", "%(TEMP_DIR)s",
                             "-r", reference],
                            IN_STDIN        = cmd_cat,
                            OUT_FREQ_3p     = os.path.join(output_directory, "3pGtoA_freq.txt"),
                            OUT_FREQ_5p     = os.path.join(output_directory, "5pCtoT_freq.txt"),
                            OUT_COMP_GENOME = os.path.join(output_directory, "dnacomp_genome.csv"),
                            OUT_COMP_USER   = os.path.join(output_directory, "dnacomp.txt"),
                            OUT_PLOT_FRAG   = os.path.join(output_directory, "Fragmisincorporation_plot.pdf"),
                            OUT_PLOT_LEN    = os.path.join(output_directory, "Length_plot.pdf"),
                            OUT_LENGTH      = os.path.join(output_directory, "lgdistribution.txt"),
                            OUT_MISINCORP   = os.path.join(output_directory, "misincorporation.txt"),
                            OUT_LOG         = os.path.join(output_directory, "Runtime_log.txt"),
                            CHECK_VERSION   = MAPDAMAGE_VERSION)

        description =  "<mapDamage: %i file(s) -> '%s'>" % (len(input_files), output_directory)
        CommandNode.__init__(self,
                             command      = ParallelCmds([cmd_cat, cmd_map]),
                             description  = description,
                             dependencies = dependencies)


class MapDamageRescaleNode(CommandNode):
    def __init__(self, config, reference, input_files, output_file, dependencies):
        cmd_cat = _concatenate_input_bams(config, input_files)
        cmd_map = AtomicCmd(["mapDamage",
                            "-n", _MAPDAMAGE_MAX_READS,
                             "-i", "-",
                             "-d", "%(TEMP_DIR)s",
                             "-r", reference],
                            IN_STDIN        = cmd_cat,
                            CHECK_VERSION   = MAPDAMAGE_VERSION)
        train_cmds = ParallelCmds([cmd_cat, cmd_map])

        cmd_cat   = _concatenate_input_bams(config, input_files)
        cmd_scale = AtomicCmd(["mapDamage", "--rescale-only",
                               "-n", _MAPDAMAGE_MAX_READS,
                               "-i", "-",
                               "-d", "%(TEMP_DIR)s",
                               "-r", reference,
                               "--rescale-out", "%(OUT_BAM)s"],
                               IN_STDIN        = cmd_cat,
                               OUT_BAM         = output_file,
                               CHECK_VERSION   = MAPDAMAGE_VERSION)
        rescale_cmds = ParallelCmds([cmd_cat, cmd_scale])

        description =  "<mapDamageRescale: %i file(s) -> '%s'>" % (len(input_files), output_file)
        CommandNode.__init__(self,
                             command      = SequentialCmds([train_cmds, rescale_cmds]),
                             description  = description,
                             dependencies = dependencies)

    def _teardown(self, config, temp):
        for filename in os.listdir(temp):
            if filename.endswith(".txt") or filename.endswith(".pdf") or filename.endswith(".csv"):
                if not filename.startswith("pipe_"):
                    os.remove(os.path.join(temp, filename))
        CommandNode._teardown(self, config, temp)


class FilterUniqueBAMNode(CommandNode):
    def __init__(self, config, input_bams, output_bam, dependencies = ()):
        merge_jar  = os.path.join(config.jar_root, "MergeSamFiles.jar")
        merge_call = ["java", "-jar", merge_jar, 
                      "TMP_DIR=%s" % config.temp_root, 
                      "SO=coordinate",
                      "QUIET=true",
                      "COMPRESSION_LEVEL=0",
                      "OUTPUT=/dev/stdout"]
        merge_files = {"OUT_STDOUT" : AtomicCmd.PIPE}
        for (ii, filename) in enumerate(input_bams, start = 1):
            merge_call.append("INPUT=%%(IN_FILE_%i)s" % ii)
            merge_files["IN_FILE_%i" % ii] = filename


        merge = AtomicCmd(merge_call, **merge_files)
        filteruniq = AtomicCmd(["FilterUniqueBAM", "--PIPE", "--library"],
                               IN_STDIN   = merge,
                               OUT_STDOUT = output_bam)

        command     = ParallelCmds([merge, filteruniq])
        description =  "<FilterUniqueBAM: %s>" % (self._desc_files(input_bams),)
        CommandNode.__init__(self, 
                             command      = command,
                             description  = description,
                             dependencies = dependencies)


class IndexAndValidateBAMNode(MetaNode):
    def __init__(self, config, node, log_file = None):
        input_file, has_index = self._get_input_file(node)
        subnodes, dependencies = [node], node.dependencies
        if not has_index:
            node = BAMIndexNode(infile       = input_file,
                                dependencies = node)
            subnodes.append(node)

        validation_params = ValidateBAMNode.customize(config       = config,
                                                      input_bam    = input_file,
                                                      output_log   = log_file,
                                                      dependencies = node)
        # Ignored since we filter out misses and low-quality hits during mapping, which
        # leads to a large proportion of missing mates for PE reads.
        validation_params.command.push_parameter("IGNORE", "MATE_NOT_FOUND", sep = "=")
        # Ignored due to high rate of false positives for lanes with few hits, where
        # high-quality reads may case ValidateSamFile to mis-identify the qualities
        validation_params.command.push_parameter("IGNORE", "INVALID_QUALITY_FORMAT", sep = "=")
        subnodes.append(validation_params.build_node())

        description = "<w/Validation: " + str(subnodes[0])[1:]
        MetaNode.__init__(self,
                          description  = description,
                          subnodes     = subnodes,
                          dependencies = dependencies)


    def _get_input_file(cls, node):
        if isinstance(node, MetaNode):
            for subnode in node.subnodes:
                input_filename, has_index = cls._get_input_file(subnode)
                if input_filename:
                    return input_filename, has_index

        input_filename, has_index = None, False
        for filename in node.output_files:
            if filename.lower().endswith(".bai"):
                has_index = True
            elif filename.lower().endswith(".bam"):
                input_filename = filename

        return input_filename, has_index


class CleanupBAMNode(CommandNode):
    def __init__(self, config, reference, input_bam, output_bam, tags, min_mapq = 0, dependencies = ()):
        flt = AtomicCmd(["samtools", "view", "-bu", "-F0x4", "-q%i" % min_mapq, "%(IN_BAM)s"],
                        IN_BAM  = input_bam,
                        OUT_STDOUT = AtomicCmd.PIPE)

        jar_file = os.path.join(config.jar_root, "AddOrReplaceReadGroups.jar")
        params = AtomicJavaParams(config, jar_file)
        params.set_parameter("INPUT", "/dev/stdin", sep = "=")
        params.set_parameter("OUTPUT", "/dev/stdout", sep = "=")
        params.set_parameter("QUIET", "true", sep = "=")
        params.set_parameter("COMPRESSION_LEVEL", "0", sep = "=")

        for (tag, value) in sorted(tags.iteritems()):
            if tag not in ("PG", "Target"):
                params.set_parameter(tag, value, sep = "=")

        params.set_paths(IN_STDIN   = flt,
                         OUT_STDOUT = AtomicCmd.PIPE)
        annotate = params.finalize()

        calmd = AtomicCmd(["samtools", "calmd", "-b", "-", "%(IN_REF)s"],
                          IN_REF   = reference,
                          IN_STDIN = annotate,
                          OUT_STDOUT = output_bam)

        description =  "<Cleanup BAM: %s -> '%s'>" \
            % (input_bam, output_bam)
        CommandNode.__init__(self,
                             command      = ParallelCmds([flt, annotate, calmd]),
                             description  = description,
                             dependencies = dependencies)


def _concatenate_input_bams(config, input_bams):
    jar_file = os.path.join(config.jar_root, "MergeSamFiles.jar")
    params = AtomicJavaParams(config, jar_file)

    params.set_paths(OUT_STDOUT = AtomicCmd.PIPE)
    params.set_parameter("OUTPUT", "/dev/stdout", sep = "=")
    params.set_parameter("CREATE_INDEX", "False", sep = "=")

    for (index, filename) in enumerate(safe_coerce_to_tuple(input_bams), start = 1):
        params.push_parameter("I", "%%(IN_BAM_%02i)s" % index, sep = "=")
        params.set_paths("IN_BAM_%02i" % index, filename)

    params.set_parameter("SO", "coordinate", sep = "=", fixed = False)

    return params.finalize()
