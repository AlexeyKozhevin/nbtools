""" !!. """
#pylint: disable=redefined-outer-name
import sys
import argparse
from time import time, sleep
from blessed import Terminal

from .resource_formatter import NBSTAT_FORMATTER, DEVICESTAT_FORMATTER, GPUSTAT_FORMATTER
from .resource_inspector import ResourceInspector



NAME_TO_FORMATTER = {
    'nbstat' : NBSTAT_FORMATTER,
    'devicestat' : DEVICESTAT_FORMATTER,
    'gpustat' : GPUSTAT_FORMATTER,
}

def main(name, interval=None):
    """ !!. """
    # Attach SIGPIPE handler to properly handle broken pipe
    try: # sigpipe not available under windows. just ignore in this case
        import signal # pylint: disable=import-outside-toplevel
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    except Exception: # pylint: disable=broad-except
        pass

    args = parse_args(name)
    formatter = NAME_TO_FORMATTER[name]

    # Update formatter with cmd arguments
    if args.pop('show_all'):
        formatter.include_all()

    for resource in args.pop('show'):
        formatter[resource] = True

    for resource in args.pop('hide'):
        formatter[resource] = False

    # Create handler to get formatted table
    inspector = ResourceInspector(formatter)
    partial_function = lambda: inspector.get_view(name, **args)

    # Print table
    interval = args.pop('interval') or interval
    if not interval:
        output_once(partial_function)
    else:
        output_looped(partial_function, interval=interval)

def parse_args(name):
    """ !!. """
    # add show-all
    argv = list(sys.argv[1:])

    parser = argparse.ArgumentParser()
    parser.add_argument('-i', '--interval', '-n', '--watch', nargs='?', type=float, default=0,
                        help='!!.')

    parser.add_argument('-f', action='store_true', default=False, dest='full_notebooks', help='!!.')
    parser.add_argument('-F', action='store_true', default=False, dest='all_processes', help='!!.')

    parser.add_argument('--show-all', action='store_true', default=False, help='!!.')
    parser.add_argument('--show', nargs='*', default=[], help='!!.')
    parser.add_argument('--hide', nargs='*', default=[], help='!!.')

    parser.add_argument('--no-supheader', action='store_false', default=True, dest='add_supheader',
                        help='!!.')
    parser.add_argument('--no-header', action='store_false', default=True, dest='add_header',
                        help='!!.')
    parser.add_argument('--no-separator', action='store_false', default=True, dest='add_separator',
                        help='!!.')
    parser.add_argument('--show-similar', action='store_false', default=True, dest='hide_similar',
                        help='!!.')
    parser.add_argument('--process-memory-format', type=str, default='GB', help='!!.')
    parser.add_argument('--device-memory-format', type=str, default='MB', help='!!.')

    parser.add_argument('index_condition', nargs='?', default=None)
    args = vars(parser.parse_args(argv))

    if args['add_separator'] is False:
        args['separate_supheader'] = False
        args['separate_header'] = False

    if args.pop('full_notebooks'):
        args['only_device_processes'] = False
        args['at_least_one_device'] = True
    if args.pop('all_processes'):
        args['only_device_processes'] = False
        args['at_least_one_device'] = False

    _ = name

    return args


def output_once(partial_function):
    """ !!. """
    try:
        view = partial_function()
        print(view)
    except Exception as e: # pylint: disable=broad-except
        _ = e
        print('Error on getting system information!' + str(e))
        raise e

def output_looped(partial_function, interval=0.5):
    """ !!. """
    terminal = Terminal()

    with terminal.fullscreen():
        try:
            counter = 0
            while True:
                counter += 1
                try:
                    start_time = time()
                    view = partial_function()
                    start_position = terminal.clear if counter % 10 == 0 else terminal.move(0, 0)
                    print(start_position, view, ' ', terminal.clear_eol, sep='')

                    remaining_time = interval - (time() - start_time)
                    if remaining_time > 0.0:
                        sleep(remaining_time)

                except Exception as e: # pylint: disable=broad-except
                    _ = e
                    sys.stderr.write('Error on getting system information!' + str(e))
                    sys.exit(1)

        except KeyboardInterrupt:
            return 0

def nbstat():
    """ !!. """
    return main('nbstat')

def nbwatch():
    """ !!. """
    return main('nbstat', interval=1.)

def devicestat():
    """ !!. """
    return main('devicestat')

def devicewatch():
    """ !!. """
    return main('devicestat', interval=1.)

if __name__ == '__main__':
    nbwatch()
