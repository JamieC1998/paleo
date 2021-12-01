# This script analyze neural network architectures.

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import logging
import os

import click
import numpy as np

from paleo import __version__
from paleo.graph import OperationGraph
from paleo import device
from paleo import profilers
from paleo import simulation
from paleo.utils import save_layer
from paleo import comm

FORMAT = "%(levelname)s %(pathname)s:%(lineno)d] %(message)s"
logging.basicConfig(format=FORMAT)
logger = logging.getLogger("paleo")
logger.setLevel(logging.INFO)


class Profiler():
    def __init__(self, filename, separator='\t'):
        """Initialize a profiler for the given network architecture."""
        self._filename = filename

        # Parse the net spec and flatten into a list in topology order.
        self.graph = OperationGraph(filename)
        logger.debug('Net spec loaded from %s.' % filename)
        logger.debug('Dependencies: %s' % str(self.graph.nested_list))
        self._separator = separator

    def print_static_summary(self):
        """Print a static summary about the network."""
        print('A summary of static characteristics of network.')
        print('  LAYER\tOUTPUTS')
        num_params = 0
        weights_in_bytes = 0
        num_activations = 0
        for layer_spec in self.graph.topology_order:
            layer = layer_spec.layer_op
            print('  %s' % layer)
            num_params += layer.num_params
            weights_in_bytes += layer.weights_in_bytes
            num_activations += np.prod(layer.outputs)
        print('Number of params: {:,} ({:,} Bytes)'.format(num_params,
                                                           weights_in_bytes))
        print('Activation: {:,} Bytes'.format(num_activations * 4))

    def save_conv_layers(self, save_dir):
        """Save convolution layers into separate files."""
        for layer_spec in self.graph.topology_order:
            if layer_spec['type'] != 'Convolution':
                continue
            layer = layer_spec.layer_op
            outfilename = os.path.join(save_dir, "%s.json" % layer_spec.name)
            save_layer.save_conv_layer(outfilename, layer)

    def profile(self, device_name, options, executor=None):
        """Profile the network with the given device spec.

        Returns:
            A dictionary contains the following keys:
              (layers, flops, executor, executor_std, flops_message,
              executor_msg)
        """
        device_spec = device.DEVICES[device_name]
        logger.info('Profiling for device %s' % device_spec.name)

        results = []
        for layer_spec in self.graph.topology_order:
            layer = layer_spec.layer_op

            # Always run flop-based profiler.
            if executor == 'tensorflow':
                # Here we disable the cudnn heuristics.
                # Tensorflow requires creating a cuda stream and does not allow
                # multiple context under one process.
                # We cannot use cuda stream because of the python wrapper.
                options.use_cudnn_heuristics = False

            flops_profiler = profilers.FlopsProfiler(options, device_spec)
            flop_based_time, output_size = flops_profiler.profile(layer)

            logger.info('Layer: %s' % layer_spec.name)
            logger.info('- %s: %s  %s' % (flops_profiler.name, flop_based_time,
                                          flops_profiler.message))
            results.append(
                (layer_spec.name, flop_based_time.total_time, output_size))
        return results

    def profile_full_pass(self, device, num_warmup, num_iter, batch_size):
        """Profile full pass execution with tensorflow."""
        options = profilers.ProfilerOptions()
        options.num_warmup = num_warmup
        options.num_iter = num_iter
        options.include_bias_and_activation = False
        from profilers.tensorflow_profiler import TensorFlowProfiler
        profiler = TensorFlowProfiler(options)

        if batch_size:
            for l in self.graph.topology_order:
                l.layer_op.batch_size = batch_size

        layers = [
            layer_spec.layer_op for layer_spec in self.graph.topology_order
        ]

        return profiler.profile_full_pass(layers)

    def simulate(self, device_name, network_name, batch_size, use_pipeline,
                 use_only_gemm, worker_sizes, scaling, ppp_comp, ppp_comm,
                 parallel, hybrid_workers):
        device_spec = device.DEVICES[device_name]
        network_spec = device.NETWORKS[network_name]

        if parallel == 'data':
            for scaling_option in scaling.split(','):
                # Estimate time for weights update.
                # Weak scaling.
                print('=' * 10)
                headers, scaling_times = simulation.simulate_scaling(
                    self.graph.nested_list, self.graph.topology_order,
                    worker_sizes, scaling_option, batch_size, device_spec,
                    network_spec, use_pipeline, use_only_gemm, ppp_comp,
                    ppp_comm)
                print('%s scaling' % scaling_option)
                print('Profiling for device %s and %s (%f GB/s)' %
                      (device_spec.name, network_spec.name,
                       network_spec.bandwidth / 8))
                print('Use pipelining: %s' % use_pipeline)
                print('Use gemm: %s  PPP comp: %f   PPP comm: %f' %
                      (use_only_gemm, ppp_comp, ppp_comm))
                print(self._separator.join(headers))
                for times in scaling_times:
                    print(self._separator.join([str(t) for t in times]))
                # return scaling_times
        elif parallel == 'model':
            # Estimate time for weights update.
            # Weak scaling.
            print('=' * 10)
            print('Model parallel')
            headers, result_times = simulation.simulate_model_parallel(
                self.graph.nested_list, self.graph.topology_order, batch_size,
                device_spec, network_spec, use_pipeline, use_only_gemm,
                ppp_comp, ppp_comm)
            print('Profiling for device %s and %s (%f GB/s)' %
                  (device_spec.name, network_spec.name,
                   network_spec.bandwidth / 8))
            print('Use pipelining: %s' % use_pipeline)
            print('Use gemm: %s  PPP comp: %f   PPP comm: %f' %
                  (use_only_gemm, ppp_comp, ppp_comm))
            print(self._separator.join(headers))
            for times in result_times:
                print(self._separator.join([str(t) for t in times]))
        elif parallel == 'hybrid':
            # Estimate time for weights update.
            print('=' * 10)
            print('Hybrid parallel')
            headers, result_times = simulation.simulate_hybrid_parallel(
                self.graph.nested_list, self.graph.topology_order, batch_size,
                device_spec, network_spec, use_pipeline, use_only_gemm,
                ppp_comp, ppp_comm, hybrid_workers)
            print('Profiling for device %s and %s (%f GB/s)' %
                  (device_spec.name, network_spec.name,
                   network_spec.bandwidth / 8))
            print('Use pipelining: %s' % use_pipeline)
            print('Hybrid workers: %d' % hybrid_workers)
            print('Use gemm: %s  PPP comp: %f   PPP comm: %f' %
                  (use_only_gemm, ppp_comp, ppp_comm))
            print(self._separator.join(headers))
            for times in result_times:
                print(self._separator.join([str(t) for t in times]))


class BaseProfiler(object):
    """API for creating customized profilers."""

    def __init__(self, filename, separator='\t'):
        """Initialize a profiler for the given network architecture."""
        self.graph = OperationGraph(filename)
        self._options = {
            'device_name': 'K80',
            'network_name': 'ethernet20',
            'use_only_gemm': True,
            'use_pipeline': False,
            'ppp_comp': 0.62,
            'ppp_comm': 0.72
        }

    @property
    def device_spec(self):
        return device.DEVICES[self._options['device_name']]

    @property
    def network_spec(self):
        return device.NETWORKS[self._options['network_name']]

    def estimate_forward(self, batch_sizes):
        forward_times, params_in_bytes = simulation._profile_for_batch_size(
            self.graph.topology_order, 'forward', self.device_spec,
            batch_sizes, self._options['use_only_gemm'],
            self._options['ppp_comp'], self._options['ppp_comm'])

        if self._options['use_pipeline']:
            return sum([t.lowerbound for t in forward_times]), params_in_bytes

        return sum(forward_times).total_time, params_in_bytes

    def estimate_backward(self, batch_sizes):
        backward_times, _ = simulation._profile_for_batch_size(
            self.graph.topology_order, 'backward', self.device_spec,
            batch_sizes, self._options['use_only_gemm'],
            self._options['ppp_comp'], self._options['ppp_comm'])

        if self._options['use_pipeline']:
            return sum([t.lowerbound for t in backward_times])
        return sum(backward_times).total_time

    def estimate_update(self, params_in_bytes):
        time_apply_updates = simulation._profile_for_apply_updates(
            params_in_bytes, self.device_spec)
        if self._options['use_pipeline']:
            return time_apply_updates.lowerbound
        return time_apply_updates.total_time

    def estimate_comm(self, workers, params_in_bytes, scheme='TreeAllReduce'):
        comm_scheme = comm.get_comm_scheme(scheme, workers, self.network_spec,
                                           self._options['ppp_comm'])
        return comm_scheme.all_reduce(params_in_bytes)


HELP_VERBOSE = 'Whether to display debug level log messages.'
HELP_DEVICE_NAME = 'Device to estimate.'


@click.group()
@click.option('--verbose', is_flag=True, help=HELP_VERBOSE)
@click.version_option(__version__, prog_name='Paleo')
def cli(verbose):
    if verbose:
        logger.setLevel(logging.DEBUG)


@cli.command()
@click.argument('netspec_files', nargs=-1)
@click.option('--device_name', default='TITAN_X', help=HELP_DEVICE_NAME)
@click.option('--network_name', default='ethernet')
@click.option('--batch_size', default=128)
@click.option('--use_pipeline', is_flag=True)
@click.option('--use_only_gemm', is_flag=True)
@click.option('--num_workers', default='1,2,4,8,16,32,64,128')
@click.option('--scaling', default='weak,strong')
@click.option('--ppp_comp', default=1.0)
@click.option('--ppp_comm', default=1.0)
@click.option('--separator', default='\t')
@click.option(
    '--parallel',
    default='data',
    type=click.Choice(['data', 'model', 'hybrid']))
@click.option('--hybrid_workers', default=1)
def simulate(netspec_files, device_name, network_name, batch_size,
             use_pipeline, use_only_gemm, num_workers, scaling, ppp_comp,
             ppp_comm, parallel, hybrid_workers, separator):
    """Simulate distributed training of a neural network."""

    num_workers = [int(x) for x in num_workers.split(',')]

    for netspec_file in netspec_files:
        print(netspec_file)
        profiler = Profiler(netspec_file, separator=separator)
        profiler.simulate(device_name, network_name, batch_size, use_pipeline,
                          use_only_gemm, num_workers, scaling, ppp_comp,
                          ppp_comm, parallel, hybrid_workers)


HELP_EXECUTIOR = 'Which executor to use.'
HELP_WARMUP = 'Iterations to burn in.'
HELP_ITER = 'Iterations to run for profiling.'
HELP_EXTRACT_CONV_DIR = 'Path to extract conv layers.'


@cli.command()
@click.argument('netspec_files', nargs=-1)
@click.option('--device_name', default='TITAN_X', help=HELP_DEVICE_NAME)
@click.option('--num_warmup', default=10, help=HELP_WARMUP)
@click.option('--num_iter', default=50, help=HELP_ITER)
@click.option('--batch_size', type=int)
def fullpass(netspec_files, device_name, num_warmup, num_iter, batch_size):
    """Profile full pass with TensorFlow."""
    for netspec_file in netspec_files:
        profiler = Profiler(netspec_file)
        fwd_time, softmax_time, bwd_time = profiler.profile_full_pass(
            device_name, num_warmup, num_iter, batch_size)
        print('Fullpass profiling with Tensorflow. Customize batch size = %s' %
              str(batch_size))
        print('Forward time\t %s' % fwd_time.total_time)
        print('Backward time\t %s' % bwd_time.total_time)
        print('Softmax time\t %s' % softmax_time.total_time)


@cli.command()
@click.argument('netspec_files', nargs=-1)
@click.option('--device_name', default='TITAN_X', help=HELP_DEVICE_NAME)
@click.option('--num_warmup', default=10, help=HELP_WARMUP)
@click.option('--num_iter', default=50, help=HELP_ITER)
@click.option('--extract_conv_dir', help=HELP_EXTRACT_CONV_DIR)
@click.option('--direction', default='forward')
@click.option('--gradient_wrt', default='data')
@click.option('--use_only_gemm', is_flag=True)
@click.option('--ppp_comp', default=1.0)
@click.option('--executor')
@click.option('--separator', default='\t')
def profile(netspec_files, device_name, num_warmup, num_iter, extract_conv_dir,
            direction, gradient_wrt, use_only_gemm, executor, ppp_comp,
            separator):
    """Profiling a neural network."""

    def _print_tabular(net_result):
        print(separator.join(
            ['layer', 'estimated (ms)', 'output size (B)']))
        sum_ours = 0
        for (layer_name, estimated_time, output) in net_result:
            #if layer_name == 'data':
            #    continue

            sum_ours += estimated_time

            print(separator.join([
                str(x)
                for x in (layer_name, estimated_time, output)
            ]))
        print(separator.join(['Sum', str(sum_ours)]))

    all_results = dict()
    for netspec_file in netspec_files:
        profiler = Profiler(netspec_file, separator=separator)

        if extract_conv_dir:
            profiler.save_conv_layers(extract_conv_dir)

        if profile:
            options = profilers.ProfilerOptions()
            options.direction = direction
            options.gradient_wrt = gradient_wrt
            options.num_iter = num_iter
            options.num_warmup = num_warmup
            options.ppp_comp = ppp_comp

            options.use_cudnn_heuristics = False
            result = profiler.profile(device_name, options)

            all_results[netspec_file] = result

    for net in all_results:
        print('Network: %s' % net)
        print('Direction: %s' % direction)
        if direction == 'backward':
            print('Gradient wrt: %s' % gradient_wrt)
        _print_tabular(all_results[net])


@cli.command()
@click.argument('netspec_files', nargs=-1)
def summary(netspec_files):
    """Summarize a neural network."""
    for netspec_file in netspec_files:
        profiler = Profiler(netspec_file)
        profiler.print_static_summary()


if __name__ == '__main__':
    cli()
