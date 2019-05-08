import os
import csv
import time
from collections import defaultdict
from google.cloud import storage
from instance import *
from scheduler import Scheduler
from hummingbird_utils import *
try:
    from urllib import unquote # python2
except ImportError:
    from urllib.parse import unquote # python3
# COMMAND = '''
# dsub \
# --provider google-v2 \
# --project gbsc-gcp-project-cba \
# --regions us-west1 \
# --image vandhanak/broadcloudgatk36gatk37-cgs:v1 \
# --command 'seqtk sample ${INPUT_FILE} ${COUNT} > ${OUTPUT_FILE}' \
# --tasks dsub.tsv
# '''
MACHINE_TYPE_PREFIX = 'n1-highmem-'

class Profiler(object):
    """Profile and collect the results for subsampled input data.

    Attributes:
        tool: A string describing downsample methods.
        conf: A dictionary of configrations.
        mode: Option for memory of runtime profiling.
        client: Cloud storage client instance.
    """
    def __init__(self, backend, tool, mode, conf):
        if backend == 'cromwell':
            self.profiler = CromwellProfiler(mode, conf)
        else:
            self.profiler = BashProfiler(mode, conf)
        self.tool = tool
        self.mode = mode
        self.conf = conf
        self.client = storage.Client(project=conf['Platform']['project'])

    def profile(self, input_dict, machines=None):
        """Perform profiling on given input and collect result from cloud
            storage bucket.

        Args:
            input_dict: A dict mapping the number of downsampled size to a list of
                downsampled filenames.

        Returns:
            A dict mapping the number of downsampled size to a list of
                memory/time usage sizes in the same order of threads specified in
                config file.
        """
        if machines is None: # do nothing with empty list
            machines = list()
            thread_list = self.conf['Profiling'].get('thread', [4])
            for thread in thread_list:
                #thread = thread.strip()
                machines.append(GCP_Instance(MACHINE_TYPE_PREFIX + str(thread), thread, None))
        if self.mode == 'mem':
            mode_str = '"%M"'
        elif self.mode == 'time':
            mode_str = '"%e"'
        if self.tool == 'time':
            result_dict = self.profiler.profile(mode_str, input_dict, machines)

        bucket = self.client.get_bucket(self.conf['Platform']['bucket'])
        profiling_dict = dict()
        for entry_count in result_dict:
            dir_list = result_dict[entry_count]
            for dir_prefix in dir_list:
                iter = bucket.list_blobs(prefix=dir_prefix)
                for blob in iter:
                    res_set = float(blob.download_as_string())
                    basename = os.path.basename(unquote(blob.path))
                    taskname, _ = os.path.splitext(basename)
                    if taskname not in profiling_dict:
                        profiling_dict[taskname] = defaultdict(list)
                    profiling_dict[taskname][entry_count].append(res_set)
        return profiling_dict

class BaseProfiler(object):
    def __init__(self, mode, conf):
        self.mode = mode
        self.conf = conf

class CromwellProfiler(BaseProfiler):
    def profile(self, format, input_dict, machines):
        result_dict = defaultdict(list)
        with open('cromwell.sh', 'w') as script:
            CMD = '''apt-get -qq update
apt-get -qq install time
wget -nv -c https://github.com/broadinstitute/cromwell/releases/download/34/cromwell-34.jar
echo '{"final_workflow_outputs_dir": "/final_outputs"}' > options.json
java -Dconfig.file=${BACKEND_CONF} -jar cromwell-34.jar run ${WDL_FILE} --inputs ${INPUT_JSON} -m meta.json -o options.json
mkdir /call_logs
for path in $(grep -Po '(?<="callRoot": ").*(?=")' meta.json)
do
    call=$(grep -Po '(?<=\/call-)[^\/]*' <<< $path)
    mkdir /call_logs/$call
    grep -Po '^(\d*\.)?\d+' $path/execution/stderr.background > /call_logs/$call.txt
done
cp -r -T /call_logs ${RESULT_DIR}
'''
            script.write(CMD)
        bucket_base = 'gs://' + self.conf['Platform']['bucket'] + '/'
        backend_conf = bucket_base + self.conf['Profiling']['backend_conf']
        wdl_file = bucket_base + self.conf['Profiling']['wdl_file']
        log_path = bucket_base + self.conf['Profiling']['logging']
        procs = []
        to_delete = []
        for i, machine in enumerate(machines):
            scheduler = Scheduler('dsub', self.conf)
            scheduler.add_argument('--script', 'cromwell.sh')
            tsv_filename = 'cromwell_profiling_' + machine.name + '.tsv'
            to_delete.append(tsv_filename)
            json_inputs = self.conf['Profiling']['json_input'][i]
            with open(tsv_filename, 'w') as dsub_tsv:
                tsv_writer = csv.writer(dsub_tsv, delimiter='\t')
                headline = ['--input BACKEND_CONF',
                            '--input WDL_FILE',
                            '--input INPUT_JSON',
                            '--output-recursive RESULT_DIR']
                tsv_writer.writerow(headline)
                for entry_count, json_input in zip(input_dict, json_inputs):
                    json_input = bucket_base + json_input
                    result_prefix = self.conf['Profiling']['result'] + '/' + humanize(str(entry_count)) + '/' + machine.name + '/' + self.mode + '/'
                    result_dict[entry_count].append(result_prefix)
                    result_dir = bucket_base + result_prefix
                    row = [backend_conf, wdl_file, json_input, result_dir]
                    tsv_writer.writerow(row)
            scheduler.add_argument('--image', self.conf['Profiling']['image'])
            scheduler.add_argument('--tasks', tsv_filename)
            scheduler.add_argument('--logging', log_path)
            scheduler.add_argument('--machine-type', machine.name)
            scheduler.add_argument('--wait')
            scheduler.add_argument('--skip')
            proc = scheduler.run()
            procs.append(proc)
            time.sleep(1)

        for proc in procs:
            proc.wait()
        for file in to_delete:
            os.remove(file)
        return result_dict

class BashProfiler(BaseProfiler):
    def profile(self, format, input_dict, machines):
        """Perform profiling on given input.

        Args:
            input_dict: A dict mapping the number of downsampled size to a list of
                downsampled filenames.

        Returns:
            A dict mapping the number of downsampled size to a list of
                paths of the results of memory/time usage profiling.
        """
        url_base = 'gs://' + self.conf['Platform']['bucket'] + '/'
        log_path = url_base + self.conf['Profiling']['logging']
        script_name = 'profiling.sh'
        result_dict = defaultdict(list)

        # Prepare profiling script
        if self.conf['Profiling'].get('script'):
            if self.conf['Profiling']['script'] == script_name:
                _, ext = os.path.splitext(script_name)
                script_name = os.path.basename(script_name) + '_' + ext
            with open(self.conf['Profiling']['script'], 'r') as ori, open(script_name, 'w') as copy:
                copy.write('apt-get -qq update\n')
                copy.write('apt-get -qq install time\n')
                for line in ori:
                    copy.write('/usr/bin/time -f {} -o ${{RESULT_FILE}} -a '.format(format) + line + '\n')
        else:
            with open(script_name, 'w') as script:
                script.write('apt-get -qq update\n')
                script.write('apt-get -qq install time\n')
                for line in self.conf['Profiling']['command'].splitlines():
                    script.write('/usr/bin/time -f {} -o ${{RESULT_FILE}} -a '.format(format) + line + '\n')

        # Prepare dsub tsv file and run
        # dsub only one type of machine at a time
        # each machine type will have an individual tsv
        procs = []
        to_delete = []
        def add_headline(flag):
            if flag in self.conf["Profiling"]:
                for key in self.conf["Profiling"][flag]:
                    headline.append('--' + flag + ' ' + key)
        for machine in machines:
            #thread = thread.strip()
            scheduler = Scheduler('dsub', self.conf)
            scheduler.add_argument('--script', script_name)
            tsv_filename = 'profiling_' + machine.name + '.tsv'
            to_delete.append(tsv_filename)
            with open(tsv_filename, 'w') as dsub_tsv:
                tsv_writer = csv.writer(dsub_tsv, delimiter='\t')
                headline = ['--env THREAD', '--output RESULT_FILE']
                for key in self.conf["Downsample"]["input"]:
                    headline.append('--input ' + key)
                add_headline('input')
                add_headline('input-recursive')
                add_headline('output')
                tsv_writer.writerow(headline)

                for entry_count in input_dict:
                    result_path = self.conf['Profiling']['result'] + '/' + humanize(str(entry_count)) + '/' + machine.name + '/' + self.mode + '/'
                    result_dict[entry_count].append(result_path)
                    result_addr = url_base + result_path + 'script.txt'
                    row = [str(machine.get_core()), result_addr] + input_dict[entry_count]
                    if 'input' in self.conf['Profiling']:
                        for path in self.conf['Profiling']['input'].values():
                            row.append(url_base + path)
                    if 'input-recursive' in self.conf['Profiling']:
                        for path in self.conf['Profiling']['input-recursive'].values():
                            row.append(url_base + path)
                    if 'output' in self.conf['Profiling']:
                        for path in self.conf['Profiling']['output'].values():
                            basename, ext = os.path.splitext(path)
                            path = basename + '_' + str(machine.get_core()) + '_' + humanize(str(entry_count)) + ext
                            row.append(url_base + path)
                    tsv_writer.writerow(row)

            scheduler.add_argument('--image', self.conf['Profiling']['image'])
            scheduler.add_argument('--tasks', tsv_filename)
            scheduler.add_argument('--logging', log_path)
            scheduler.add_argument('--machine-type', machine.name)
            scheduler.add_argument('--wait')
            scheduler.add_argument('--skip')
            proc = scheduler.run()
            procs.append(proc)
            time.sleep(1)

        for proc in procs:
            proc.wait()
        for file in to_delete:
            os.remove(file)
        return result_dict

# COMMAND = '''
# dsub \
# --provider google-v2 \
# --project gbsc-gcp-project-cba \
# --regions us-west1 \
# --image quay.io/encode-dcc/atac-seq-pipeline:v1.1.3 \
# --script cromwell.sh

# '''
