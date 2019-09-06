#!/usr/bin/python
#
# Copyright (c) 2012 Mikkel Schubert <MikkelSch@gmail.com>
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
#
from __future__ import print_function

import os
import logging

import paleomix
import paleomix.logger
import paleomix.resources
import paleomix.yaml

from paleomix.pipeline import \
    Pypeline
from paleomix.nodes.picard import \
    BuildSequenceDictNode
from paleomix.nodes.samtools import \
    FastaIndexNode
from paleomix.nodes.bwa import \
    BWAIndexNode
from paleomix.nodes.bowtie2 import \
    Bowtie2IndexNode
from paleomix.nodes.validation import \
    ValidateFASTAFilesNode

from paleomix.tools.bam_pipeline.makefile import \
    MakefileError, \
    read_makefiles

from paleomix.tools.bam_pipeline.parts import \
    Reads

import paleomix.tools.bam_pipeline.parts as parts
import paleomix.tools.bam_pipeline.config as bam_config
import paleomix.tools.bam_pipeline.mkfile as bam_mkfile


def build_pipeline_trimming(config, makefile):
    """Builds only the nodes required to produce trimmed reads.
    This reduces the required complexity of the makefile to a minimum."""

    nodes = []
    for (_, samples) in makefile["Targets"].iteritems():
        for libraries in samples.itervalues():
            for barcodes in libraries.itervalues():
                for record in barcodes.itervalues():
                    if record["Type"] in ("Raw", "Trimmed"):
                        offset = record["Options"]["QualityOffset"]
                        reads = Reads(config, record, offset)

                        nodes.extend(reads.nodes)

    return nodes


def build_pipeline_full(config, makefile, return_nodes=True):
    result = []
    features = makefile["Options"]["Features"]
    for (target_name, sample_records) in makefile["Targets"].iteritems():
        prefixes = []
        for (_, prefix) in makefile["Prefixes"].iteritems():
            samples = []
            for (sample_name, library_records) in sample_records.iteritems():
                libraries = []
                for (library_name, barcode_records) in library_records.iteritems():
                    lanes = []
                    for (barcode, record) in barcode_records.iteritems():
                        lane = parts.Lane(config, prefix, record, barcode)

                        # ExcludeReads settings may exlude entire lanes
                        if lane.bams:
                            lanes.append(lane)

                    if lanes:
                        libraries.append(parts.Library(config=config,
                                                       target=target_name,
                                                       prefix=prefix,
                                                       lanes=lanes,
                                                       name=library_name))

                if libraries:
                    samples.append(parts.Sample(config=config,
                                                prefix=prefix,
                                                libraries=libraries,
                                                name=sample_name))

            if samples:
                prefixes.append(parts.Prefix(config=config,
                                             prefix=prefix,
                                             samples=samples,
                                             features=features,
                                             target=target_name))

        if prefixes:
            target = parts.Target(config, prefixes, target_name)

            # Construct coverage, depth-histogram, and summary nodes, etc.
            parts.add_statistics_nodes(config, makefile, target)

            if return_nodes:
                # Extra tasks (e.g. coverage, depth-histograms, etc.)
                result.extend(target.nodes)
                # Output BAM files (raw, realigned)
                result.extend(target.bams.itervalues())
            else:
                result.append(target)

    return result


def index_references(config, makefiles):
    references = {}
    references_bwa = {}
    references_bowtie2 = {}
    for makefile in makefiles:
        for subdd in makefile["Prefixes"].itervalues():
            reference = subdd["Reference"]
            if reference not in references:
                # Validation of the FASTA file; not blocking for the other
                # steps, as it is only expected to fail very rarely, but will
                # block subsequent analyses depending on the FASTA.
                valid_node = ValidateFASTAFilesNode(input_files=reference,
                                                    output_file=reference +
                                                    ".validated")
                # Indexing of FASTA file using 'samtools faidx'
                faidx_node = FastaIndexNode(reference)
                # Indexing of FASTA file using 'BuildSequenceDictionary.jar'
                dict_node = BuildSequenceDictNode(config=config,
                                                  reference=reference,
                                                  dependencies=(valid_node,))

                # Indexing of FASTA file using 'bwa index'
                bwa_node = BWAIndexNode(input_file=reference,
                                        dependencies=(valid_node,))
                # Indexing of FASTA file using ''
                bowtie2_node = Bowtie2IndexNode(input_file=reference,
                                                dependencies=(valid_node,))

                references[reference] = (valid_node, faidx_node, dict_node)
                references_bwa[reference] = (valid_node, faidx_node,
                                             dict_node, bwa_node)
                references_bowtie2[reference] = (valid_node, faidx_node,
                                                 dict_node, bowtie2_node)

            subdd["Nodes"] = references[reference]
            subdd["Nodes:BWA"] = references_bwa[reference]
            subdd["Nodes:Bowtie2"] = references_bowtie2[reference]


def run(config, args, pipeline_variant):
    paleomix.logger.initialize(
        log_level=config.log_level,
        log_file=config.log_file,
        name='bam_pipeline',
    )

    logger = logging.getLogger(__name__)
    if pipeline_variant not in ("bam", "trim"):
        logger.critical("Unexpected BAM pipeline variant %r", pipeline_variant)
        return 1

    if not os.path.exists(config.temp_root):
        try:
            os.makedirs(config.temp_root)
        except OSError as error:
            logger.error("Could not create temp root: %s", error)
            return 1

    if not os.access(config.temp_root, os.R_OK | os.W_OK | os.X_OK):
        logger.error("Insufficient permissions for temp root: %r",
                     config.temp_root)
        return 1

    # Init worker-threads before reading in any more data
    pipeline = Pypeline(config)

    try:
        makefiles = read_makefiles(config, args, pipeline_variant)
    except (MakefileError, paleomix.yaml.YAMLError, IOError) as error:
        logger.error("Error reading makefiles: %s", error)
        return 1

    pipeline_func = build_pipeline_trimming
    if pipeline_variant == "bam":
        # Build .fai files for reference .fasta files
        index_references(config, makefiles)

        pipeline_func = build_pipeline_full

    for makefile in makefiles:
        logger.info("Building BAM pipeline for %r", makefile['Statistics']['Filename'])
        # If a destination is not specified, save results in same folder as the
        # makefile
        filename = makefile["Statistics"]["Filename"]
        old_destination = config.destination
        if old_destination is None:
            config.destination = os.path.dirname(filename)

        try:
            nodes = pipeline_func(config, makefile)
        except paleomix.node.NodeError, error:
            logger.error("Error while building pipeline for '%s':\n%s",
                         filename, error)
            return 1

        config.destination = old_destination

        pipeline.add_nodes(*nodes)

    if config.list_input_files:
        logger.info("Printing output files ...")
        pipeline.print_input_files()
        return 0
    elif config.list_output_files:
        logger.info("Printing output files ...")
        pipeline.print_output_files()
        return 0
    elif config.list_executables:
        logger.info("Printing required executables ...")
        pipeline.print_required_executables()
        return 0
    elif config.dot_file:
        logger.info("Writing dependency graph to %r ...", config.dot_file)
        if not pipeline.to_dot(config.dot_file):
            return 1
        return 0

    logger.info("Running BAM pipeline ...")
    if not pipeline.run(dry_run=config.dry_run,
                        max_threads=config.max_threads):
        return 1

    return 0


def _print_usage(pipeline):
    basename = "%s_pipeline" % (pipeline,)
    usage = \
        "BAM Pipeline v{version}\n" \
        "Usage:\n" \
        "  -- {cmd} help           -- Display this message.\n" \
        "  -- {cmd} example [...]  -- Create example project.\n" \
        "  -- {cmd} makefile [...] -- Print makefile template.\n" \
        "  -- {cmd} dryrun [...]   -- Perform dry run of pipeline.\n" \
        "  -- {cmd} run [...]      -- Run pipeline on provided makefiles.\n"

    print(usage.format(version=paleomix.__version__,
                       cmd=basename,
                       pad=" " * len(basename)))


def main(argv, pipeline="bam"):
    assert pipeline in ("bam", "trim"), pipeline

    commands = ("makefile", "mkfile", "run",
                "dry_run", "dry-run", "dryrun",
                "example", "examples")

    if not argv or (argv[0] == "help"):
        _print_usage(pipeline)
        return 0
    elif argv[0] not in commands:
        _print_usage(pipeline)
        return 1
    elif argv[0] in ("mkfile", "makefile"):
        return bam_mkfile.main(argv[1:], pipeline=pipeline)
    elif argv[0] in ("example", "examples"):
        return paleomix.resources.copy_example("bam_pipeline", argv[1:])

    try:
        config, args = bam_config.parse_config(argv, pipeline)

        if args and args[0].startswith("dry"):
            config.dry_run = True
    except bam_config.ConfigError as error:
        logger = logging.getLogger(__name__)
        logger.error(error)
        return 1

    return run(config, args[1:], pipeline_variant=pipeline)
