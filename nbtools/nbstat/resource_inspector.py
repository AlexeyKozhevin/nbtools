""" Controlling class for fetching tables with system information, merging them into view and displaying in stream. """
import os
import re
import json
import time
import platform
import linecache

import psutil
import requests
from blessed import Terminal

import nvidia_smi

from .resource import Resource
from .resource_table import ResourceTable
from .utils import true_len, true_rjust, format_memory


KERNEL_ID_SEARCHER   = re.compile('kernel-(.*).json').search
VSCODE_KEY_SEARCHER  = re.compile('key=b"(.*)"').search
SCRIPT_NAME_SEARCHER = re.compile('python.* (.*).py').search

SYSTEM = platform.system()

def pid_to_name_generic(pid):
    """ Get `name` of a process by its PID. Platform-agnostic. """
    try:
        process = psutil.Process(pid)
        name = process.name()
    except (psutil.AccessDenied, psutil.ZombieProcess, psutil.NoSuchProcess):
        name = ''
    return name

def pid_to_name_linux(pid):
    """ Get `name` of a process by its PID on Linux. ~20% speed-up, compared to the `generic` version. """
    try:
        line = linecache.getline(f'/proc/{pid}/status', 1)
        name = line.strip().split()[1]
    except Exception: #pylint: disable=broad-except
        name = ''
    return name

pid_to_name = pid_to_name_linux if SYSTEM == 'Linux' else pid_to_name_generic


def pid_to_ngid_generic(pid):
    """ Get NGID of a process by its PID. """
    return pid

def pid_to_ngid_linux(pid):
    """ Get NGID of a process by its PID on Linux. Used as the PID on host for a process inside a container. """
    try:
        line = linecache.getline(f'/proc/{pid}/status', 5)
        ngid = line.strip().split()[1]
        ngid = int(ngid)
    except Exception: #pylint: disable=broad-except
        ngid = pid
    return ngid or pid

pid_to_ngid = pid_to_ngid_linux if SYSTEM == 'Linux' else pid_to_ngid_generic


class ResourceInspector:
    """ A class to controll the process of gathering information about system resources into ResourceTables,
    merging them into views, and formatting into nice colored strings.

    TODO: correct working with VSCode Jupyter Notebooks
    TODO: make sure that everything works without sudo
    TODO: add more fallbacks for unavailable resources
    TODO: can add explicit __delete__ to call nvidia_smi.nvmlShutdown(), if we ever have problems with that
    """
    def __init__(self, formatter):
        self.formatter = formatter
        self._device_handles = None

        self.pid_to_process = {}

        self.warnings = {}

    @property
    def device_handles(self):
        """ Cached handles of NVIDIA devices. """
        if self._device_handles is None:
            nvidia_smi.nvmlInit()
            n_devices = nvidia_smi.nvmlDeviceGetCount()

            self._device_handles = {device_id : nvidia_smi.nvmlDeviceGetHandleByIndex(device_id)
                                    for device_id in range(n_devices)}
        return self._device_handles


    # Collect system resources into ResourceTables
    def get_device_table(self):
        """ Collect data about current device usage into two tables:
        one is indexed by device, the second is indexed by process on a device.

        Each value is collected only if requested by the current formatter.
        Device-wide values (like temperature and utilization) are reported for each process.

        As the slowest operation is getting device handles, we cache it inside the instance attributes.
        Note that this does nothing for a single query to this class.

        PIDs, reported by nvidia-smi, are from the host namespace and may (most probably) not
        exist in the container namespace. Currently, we don't have a reliable and not overly hacky way of matching it to
        a PID inside the container: we circumwent this problem in the `process_table`.
        """
        device_table, device_process_table = ResourceTable(), ResourceTable()

        for device_id, handle in self.device_handles.items():
            common_info = {Resource.DEVICE_ID : device_id,
                           Resource.DEVICE_NAME : nvidia_smi.nvmlDeviceGetName(handle).decode()}

            # Inseparable device information like memory, temperature, power, etc. Request it only if needed
            if self.formatter.get(Resource.DEVICE_UTIL, False):
                utilization = nvidia_smi.nvmlDeviceGetUtilizationRates(handle)
                common_info[Resource.DEVICE_UTIL] = utilization.gpu
                common_info[Resource.DEVICE_MEMORY_UTIL] = utilization.memory

            if self.formatter.get(Resource.DEVICE_TEMP, False):
                temperature = nvidia_smi.nvmlDeviceGetTemperature(handle, nvidia_smi.NVML_TEMPERATURE_GPU)
                common_info[Resource.DEVICE_TEMP] = temperature

            if self.formatter.get(Resource.DEVICE_FAN, False):
                fan_speed = nvidia_smi.nvmlDeviceGetFanSpeed(handle)
                common_info[Resource.DEVICE_FAN] = fan_speed

            if self.formatter.get(Resource.DEVICE_POWER_USED, False):
                power_used = nvidia_smi.nvmlDeviceGetPowerUsage(handle)
                power_total = nvidia_smi.nvmlDeviceGetEnforcedPowerLimit(handle)

                common_info[Resource.DEVICE_POWER_USED] = power_used
                common_info[Resource.DEVICE_POWER_TOTAL] = power_total

            if (self.formatter.get(Resource.DEVICE_MEMORY_USED, False) or
                self.formatter.get(Resource.DEVICE_PROCESS_MEMORY_USED, False)):
                memory = nvidia_smi.nvmlDeviceGetMemoryInfo(handle)
                common_info[Resource.DEVICE_MEMORY_USED] = memory.used
                common_info[Resource.DEVICE_MEMORY_TOTAL] = memory.total

            # Collect individual processes info, if needed. Save it to both tables: in one as list, in other separately
            device_info = {**common_info}
            processes = nvidia_smi.nvmlDeviceGetComputeRunningProcesses(handle)
            device_info.update({Resource.DEVICE_PROCESS_N : 0,
                                Resource.DEVICE_PROCESS_PID : [],
                                Resource.DEVICE_PROCESS_MEMORY_USED : []})

            if processes:
                for process in processes:
                    pid, process_memory = process.pid, process.usedGpuMemory

                    # Update the aggregate device info table
                    device_info[Resource.DEVICE_PROCESS_N] += 1
                    device_info[Resource.DEVICE_PROCESS_PID].append(pid)
                    device_info[Resource.DEVICE_PROCESS_MEMORY_USED].append(process_memory)

                    # Update the table with individual processes
                    device_process_info = {**common_info}
                    device_process_info[Resource.DEVICE_PROCESS_PID] = pid
                    device_process_info[Resource.DEVICE_PROCESS_MEMORY_USED] = process_memory
                    device_process_table.append(device_process_info)

            device_table.append(device_info)
        return device_table, device_process_table

    def get_notebook_table(self):
        """ Collect information about all running Jupyter Notebooks inside all of the Jupyter Servers.
        Works with both v2 and v3 APIs.

        The most valuable information from this table is the mapping from `kernel_id` to `path` and `name`: all of
        other properties of a process can be retrieved by looking at the process (see `get_process_table`).

        TODO: once VSCode has stable standard and doc for ipykernel launches, add its parsing here.
        """
        #pylint: disable=import-outside-toplevel
        servers = []
        try:
            from notebook.notebookapp import list_running_servers as list_running_servers_v2
            servers.extend(list(list_running_servers_v2()))
        except ImportError:
            pass
        try:
            from jupyter_server.serverapp import list_running_servers as list_running_servers_v3
            servers.extend(list(list_running_servers_v3()))
        except ImportError:
            pass

        # Information about all running kernels for all running servers
        notebook_table = ResourceTable()
        for server in servers:
            root_dir = server.get('root_dir') or server.get('notebook_dir') # for v2 and v3
            response = requests.get(requests.compat.urljoin(server['url'], 'api/sessions'),
                                    params={'token': server.get('token', '')})

            for instance in json.loads(response.text):
                kernel_id = instance['kernel']['id']
                notebook_info = {
                    Resource.NAME : instance['notebook']['name'],
                    Resource.PATH : os.path.join(root_dir, instance['notebook']['path']),
                    Resource.KERNEL : kernel_id,
                }
                notebook_table.append(notebook_info)
        return notebook_table

    def get_python_pids(self):
        """ PIDs of processes, which have `python` in its name. """
        python_pids = set()
        for pid in psutil.pids():
            name = pid_to_name(pid)
            if 'python' in name:
                python_pids.add(pid)
        return python_pids

    def get_process_table(self):
        """ Collect information about all Python processes.
        Information varies from properties of a process (its path, PID, NGID, status, etc) to system resource usage like
        CPU utilization or RSS. The table also includes some inferred columns like type and kernel_id.

        Some of the fields are intentionally left blank / with meaningless defaults: those are supposed to be filled by
        later merges / updates of the multiple tables. For example, processes that contain `kernel_id` in the name are
        matched on `kernel_id` with the result of `get_notebook_table` to fill in correct names and paths.

        If those fields are not updated with correct info, they would break table formatting: mainly, sorting and
        filtering. This is a good thing, as such occasions signal about something very wrong and unexpected.
        An example of this is an abandoned Jupyter Notebook, not managed by its Jupyter Server, or a host process, for
        some reasons visible inside the container.

        As `NBStat` can be run inside the container which has different namespace to the host, we are trying to match
        PIDs of the processes to the ones on the host. That is what the `NGID` column for: later we use
        either PIDs or NGIDs (whichever matches the DEVICE_PIDs) to merge with device information.
        This fallback should be easy to extend once we find new ways of inferring the host PID of a process.
        """
        python_pids = self.get_python_pids()

        process_table = ResourceTable()
        for pid in python_pids:
            try:
                if pid not in self.pid_to_process:
                    self.pid_to_process[pid] = psutil.Process(pid)
                process = self.pid_to_process[pid]
                pid = process.pid

                with process.oneshot():
                    # Command used to start the Python interpreter
                    cmdline = ' '.join(process.cmdline())

                    # cwd with a default: access can be denied to current user
                    try:
                        cwd = process.cwd()
                    except psutil.AccessDenied:
                        cwd = ''

                    # Determine the type, name and path of the python process
                    kernel_id = KERNEL_ID_SEARCHER(cmdline)
                    vscode_key = VSCODE_KEY_SEARCHER(cmdline)
                    script_name = SCRIPT_NAME_SEARCHER(cmdline)

                    if kernel_id:
                        # The name will be changed by data from `notebook_table`.
                        # If not, then something very fishy is going on.
                        type_ = 'notebook'
                        name = kernel_id.group(1).split('-')[0] + '.ipynb'
                        path = os.path.join(cwd, name)
                        kernel_id = kernel_id.group(1)
                    elif vscode_key:
                        # Can't tell much more for processes run by VSCode for now
                        type_ = 'vscode'
                        name = vscode_key.group(1).split('-')[0] + '.ipynb'
                        path = kernel_id = vscode_key.group(1)
                    elif script_name:
                        type_ = 'script'
                        name = script_name.group(1) + '.py'
                        path = os.path.join(cwd, name)
                        kernel_id = None
                    else:
                        type_ = 'unknown'
                        name = 'unknown'
                        path = cwd
                        kernel_id = None

                    # PYTHON_PPID = PPID if parent is Python process else -1
                    ppid = process.ppid()
                    if ppid in python_pids:
                        # Spawned by one of other Python processes
                        type_ = 'subprocess'
                        python_ppid = ppid
                    elif 'containerd' in pid_to_name(ppid):
                        # Something very wrong is going on
                        type_ = 'containerd'
                        python_ppid = ppid
                    else:
                        # Spawned by non-Python process: terminal / Jupyter Server
                        python_ppid = -1

                    # Fill in the basic info
                    process_info = {
                        Resource.PID : pid,
                        Resource.PPID : ppid,
                        Resource.NGID : pid_to_ngid(pid),
                        Resource.PYTHON_PPID : python_ppid,
                        Resource.TYPE : type_,
                        Resource.NAME : name,
                        Resource.PATH : path,
                        Resource.CREATE_TIME : process.create_time(),
                        Resource.KERNEL : kernel_id,
                        Resource.STATUS : process.status(),
                        Resource.PROCESS : process
                    }

                    # Gather resource info
                    if self.formatter.get(Resource.CPU, False):
                        process_info[Resource.CPU] = process.cpu_percent()

                    if self.formatter.get(Resource.RSS, False):
                        memory = process.memory_info()
                        process_info[Resource.RSS] = memory.rss

                process_table.append(process_info)

            except (psutil.AccessDenied, psutil.ZombieProcess, psutil.NoSuchProcess, FileNotFoundError):
                continue
        return process_table


    # Aggregate multiple ResourceTables into more representative tables
    def make_nbstat_table(self, sort=True, verbose=0):
        """ Prepare a `nbstat` view: a table, indexed by script/notebook name, with info about each of its processes.

        Parameters
        ----------
        sort : bool
            If True, then the table is sorted in the following order:
                - notebook name, which is used as table index
                - for each notebook, we display its process as the first row
                - then all device processes, sorted by device ID
                - then all other processes, sorted by create time
        verbose : {0, 1, 2}
            Sets the filtration of the table.
            If 0, then we keep only notebooks, which use at least one device. For them we keep only device processes.
            If 1, then we keep only notebooks, which use at least one device. For them we keep all processes.
            If 2, then we keep all notebooks and all processes for them.
        """
        # Collect all the data
        _, device_process_table = self.get_device_table()       # ~20% of the method time taken
        notebook_table = self.get_notebook_table()              # ~15% of the method time taken
        process_table = self.get_process_table()                # ~45% of the method time taken

        # Try to match the process pids (local namespace) to device pids (host). Merge on those
        table = process_table
        if device_process_table:
            device_pids = device_process_table[Resource.DEVICE_PROCESS_PID]

            def select_pid(entry):
                pid = entry[Resource.PID]
                ngid = entry[Resource.NGID]
                result = ngid if ngid in device_pids else pid
                return result

            table.add_column(Resource.HOST_PID, select_pid)
            self.nbstat_check_device_pids(device_pids, table, add_to_table=True)

            table = ResourceTable.merge(table, device_process_table,
                                        self_key=Resource.HOST_PID, other_key=Resource.DEVICE_PROCESS_PID)

        # Update entries: change `path` and `name` for Notebook from placeholders to proper ones
        if notebook_table:
            table.update(notebook_table, self_key=Resource.KERNEL, other_key=Resource.KERNEL, inplace=True)

        # Custom sort for processes: parent -> device processes -> non-device processes -> create time
        if sort:
            is_parent = lambda entry: entry[Resource.PYTHON_PPID] == -1
            table.add_column('is_parent', is_parent)

            table.sort(key=['is_parent', Resource.DEVICE_ID, Resource.CREATE_TIME],
                       reverse=[True, False, False], default=[0.0, 999, 0.0])

        # Filter non-device processes
        if verbose == 0:
            function = lambda entry: (entry.get(Resource.DEVICE_ID) is not None or entry[Resource.PYTHON_PPID] == -1)
            table.filter(function, inplace=True)

        # Sort index on create time
        table.set_index(Resource.PATH, inplace=True)
        if sort:
            uses_device = lambda entry: entry[Resource.DEVICE_ID] is not None
            table.add_column('uses_device', uses_device)
            table.sort_by_index(key=('uses_device', Resource.CREATE_TIME),
                                reverse=[True, False], aggregation=[max, min])

        # Filter non-device notebooks
        if verbose <= 1:
            function = lambda entry: (entry.get(Resource.DEVICE_ID) is not None)
            table.filter_by_index(function, inplace=True)
        return table

    def make_devicestat_table(self):
        """ A transposed `nbstat` view: the same information, but indexed with device ids. """
        device_table, device_process_table = self.get_device_table()
        notebook_table = self.get_notebook_table()
        process_table = self.get_process_table()

        # Try to match the process pids (local namespace) to device pids (host). Merge on those
        device_pids = device_process_table[Resource.DEVICE_PROCESS_PID]
        def select_pid(entry):
            pid = entry[Resource.PID]
            ngid = entry[Resource.NGID]
            result = ngid if ngid in device_pids else pid
            return result
        process_table.add_column(Resource.HOST_PID, select_pid)

        table = device_table.unroll(inplace=False)
        table = table.merge(process_table, self_key=Resource.DEVICE_PROCESS_PID, other_key=Resource.HOST_PID)

        # Update entries: change `path` and `name` for Notebook from placeholders to proper ones
        if notebook_table:
            table.update(notebook_table, self_key=Resource.KERNEL, other_key=Resource.KERNEL, inplace=True)

        self.devicestat_check_device_pids(table)

        # A simple sort of entries and index
        table.sort(key=Resource.CREATE_TIME, reverse=False)
        table.set_index(Resource.DEVICE_ID, inplace=True)
        table.sort_by_index(key=Resource.DEVICE_ID, aggregation=min)
        return table

    def make_gpustat_table(self):
        """ A device-only view. Same information, as vanilla `gpustat`. """
        device_table, _ = self.get_device_table()
        device_table.set_index(Resource.DEVICE_ID)
        return device_table


    # Check for information consistency in multiple tables
    def nbstat_check_device_pids(self, device_pids, table, add_to_table=True):
        """ Check if some of `device pids` are not referenced in the `table`.
        Add them with template names and values, if needed.
        """
        set_device_pids = set(device_pids)
        set_host_pids = set(table[Resource.HOST_PID])
        if None in set_device_pids:
            set_device_pids.pop(None)
        if None in set_host_pids:
            set_host_pids.pop(None)

        if set_device_pids != set_host_pids:
            missing_pids = set_device_pids.difference(set_host_pids)
            self.warnings['missing_device_pids'] = missing_pids

            if add_to_table:
                entry_template = {key : None for key in table.columns}
                for missing_pid in sorted(missing_pids):
                    entry = {
                        **entry_template,
                        Resource.NAME : 'device_zombie',
                        Resource.TYPE : 'device_zombie',
                        Resource.PATH : 'device_zombie',
                        Resource.STATUS : 'sleeping',
                        Resource.PID : missing_pid,
                        Resource.PPID : missing_pid,
                        Resource.NGID : missing_pid,
                        Resource.HOST_PID : missing_pid,
                        Resource.PYTHON_PPID : missing_pid,
                        Resource.CREATE_TIME : missing_pid, # for sort on `CREATE_TIME`
                    }
                    table.append(entry)

    def devicestat_check_device_pids(self, table):
        """ Check if some of the `device pids` are not matched to any Python processes.
        Add template names to them insted of empty ones.
        """
        self.warnings['missing_device_pids'] = set()
        for entry in table:
            if entry[Resource.DEVICE_PROCESS_PID] is not None and entry[Resource.HOST_PID] is None:
                self.warnings['missing_device_pids'].add(entry[Resource.DEVICE_PROCESS_PID])

                entry.update({Resource.NAME : 'device_zombie',
                              Resource.TYPE : 'device_zombie',
                              Resource.PATH : 'device_zombie',
                              Resource.STATUS : 'sleeping'})

    # Make formatted visualization of tables
    def get_view(self, name='nbstat', index_condition=None, force_styling=True, sort=True, verbose=0,
                 add_supheader=True, underline_supheader=True, bold_supheader=True, separate_supheader=False,
                 add_header=True, underline_header=True, bold_header=False, separate_header=True,
                 add_footnote=False, underline_footnote=False, bold_footnote=False, separate_footnote=True,
                 separate_index=True, separator='—', hide_similar=True,
                 process_memory_format='GB', device_memory_format='MB'):
        """ Get the desired view. Format it into colored string.
        Optionally, add a supheader (driver and CUDA info) and a footnote (total CPU / RSS usage) to the visualization.
        """
        # Get the table
        if name.startswith('nb'):
            table = self.make_nbstat_table(sort=sort, verbose=verbose)
        elif name.startswith('device'):
            table = self.make_devicestat_table()
        elif name.startswith('gpu'):
            table = self.make_gpustat_table()
        else:
            raise ValueError('Wrong name of view to get!')

        # Filter index of the table by a regular expression
        if table and index_condition is not None:
            function = lambda index_value, _: bool(re.search(index_condition, str(index_value)))
            table.filter_on_index(function, inplace=True)

        # Create terminal instance
        terminal = self.make_terminal(force_styling=force_styling, separator=separator)

        # Make formatted strings
        lines = table.format(terminal=terminal, formatter=self.formatter,
                             add_header=add_header, underline_header=underline_header,
                             bold_header=bold_header, separate_header=separate_header,
                             separate_index=separate_index, hide_similar=hide_similar,
                             process_memory_format=process_memory_format, device_memory_format=device_memory_format)

        if add_supheader:
            lines = self.add_supheader(lines, terminal=terminal,
                                       underline=underline_supheader, bold=bold_supheader, separate=separate_supheader)
        if add_footnote:
            lines = self.add_footnote(lines, terminal=terminal,
                                      underline=underline_footnote, bold=bold_footnote, separate=separate_footnote,
                                      process_memory_format=process_memory_format)

        # Placeholder for empty table
        if not table:
            placeholder = true_rjust(terminal.bold + 'No entries to display!' + terminal.normal, true_len(lines[-1]))
            lines[-1] = placeholder

        # For debug purposes
        self._table = table
        return '\n'.join(lines)


    def make_terminal(self, force_styling, separator):
        """ Create terminal instance. """
        terminal = Terminal(kind=os.getenv('TERM'), force_styling=force_styling if force_styling else None)
        terminal.separator_symbol = terminal.bold + separator + terminal.normal
        terminal._normal = u'\x1b[0;10m' # pylint: disable=protected-access
        return terminal

    def add_line(self, lines, parts, terminal, position, separator_position, underline, bold):
        """ Add line, created from joined `parts`, to `lines`, in desired `position`. """
        if underline:
            parts = [terminal.underline + part for part in parts]
        if bold:
            parts = [terminal.bold + part for part in parts]
        parts = [part + terminal.normal for part in parts]
        added_line = '    '.join(parts)

        added_line_width = true_len(added_line)
        table_width = true_len(lines[0])

        if added_line_width <= table_width:
            added_line = true_rjust(added_line, table_width)
        else:
            lines = [true_rjust(line, added_line_width) for line in lines]
        lines.insert(position, added_line)

        if separator_position is not None:
            lines.insert(separator_position, terminal.separator_symbol * true_len(added_line))
        return lines

    def add_supheader(self, lines, terminal, underline=True, bold=True, separate=True):
        """ Add a supheader with info about current time, driver and CUDA versions. """
        timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
        driver_version = '.'.join(nvidia_smi.nvmlSystemGetDriverVersion().decode().split('.')[:-1])
        cuda_version = nvidia_smi.nvmlSystemGetNVMLVersion().decode()[:4]

        parts = [
            timestamp,
            f'Driver Version: {driver_version}',
            f'CUDA Version: {cuda_version}'
        ]
        lines = self.add_line(lines=lines, parts=parts, terminal=terminal,
                              position=0, separator_position=1 if separate else None,
                              underline=underline, bold=bold)
        return lines

    def add_footnote(self, lines, terminal, underline=True, bold=True, separate=True, process_memory_format='GB'):
        """ Add a footnote with info about current CPU and RSS usage. """
        vm = psutil.virtual_memory()
        vm_used, unit = format_memory(vm.used, process_memory_format)
        vm_total, unit = format_memory(vm.total, process_memory_format)
        n_digits = len(str(vm_total))

        parts = [
            f'{terminal.bold + terminal.cyan}SYSTEM CPU: {psutil.cpu_percent():6}%',
            f'{terminal.bold + terminal.cyan}SYSTEM RSS: {vm_used:>{n_digits}} / {vm_total} {unit}'
        ]

        lines = self.add_line(lines=lines, parts=parts, terminal=terminal,
                              position=len(lines), separator_position=None,
                              underline=underline, bold=bold)
        if separate:
            lines.insert(-1, ' ')
        return lines
