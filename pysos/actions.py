#!/usr/bin/env python3
#
# This file is part of Script of Scripts (sos), a workflow system
# for the execution of commands and scripts in different languages.
# Please visit https://github.com/bpeng2000/SOS
#
# Copyright (C) 2016 Bo Peng (bpeng@mdanderson.org)
##
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.
#

import os
import sys
import re
import subprocess
import tempfile
import shlex
import json
import glob
import platform
import urllib
import shutil
import pycurl
import zipfile
import gzip
import tarfile
import blessings
from io import BytesIO
from docker import Client
from collections.abc import Sequence
from docker.utils import kwargs_from_env
import multiprocessing as mp
from .utils import env, getTermWidth, ProgressBar, shortRepr
from .pattern import glob_wildcards
from .sos_eval import interpolate, Undetermined
from .signature import FileSignature, fileMD5
from .sos_executor import Sequential_Executor

__all__ = ['SoS_Action', 'execute_script', 'sos_run',
    'check_command', 'fail_if', 'warn_if', 'download',
    'run', 'bash', 'csh', 'tcsh', 'zsh', 'sh',
    'python', 'python3',
    'perl', 'ruby', 'node', 'JavaScript',
    'R', 'check_R_library',
    'docker_build', 'docker_commit',
    'report', 'pandoc', 'Rmarkdown',
    ]

from .sos_syntax import SOS_RUNTIME_OPTIONS
from .sos_script import SoS_Script

def get_actions():
    # get the name of all actions, which are identified by an attribute
    # run_mode of the function
    return [k for k, v in globals().items() if hasattr(v, 'run_mode')]

#
# docker support
#
class DockerClient:
    '''A singleton class to ensure there is only one client'''
    _instance = None
    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super(DockerClient, cls).__new__(cls)
        return cls._instance

    def __init__(self):
        kwargs = kwargs_from_env(assert_hostname=False)
        kwargs.update({'version': 'auto'})
        self.client = Client(**kwargs)
        try:
            self.client.info()
            # mount the /Volumes folder under mac, please refer to
            #    https://github.com/bpeng2000/SOS/wiki/SoS-Docker-guide
            # for details.
            self.has_volumes = False
            if platform.system() == 'Darwin':
                try:
                    # this command log in to the docker machine, check if /Volumes has been mounted,
                    # and try to mount it if possible. This requires users to configure
                    subprocess.call("""docker-machine ssh "{}" 'mount | grep /Volumes || {{ echo "mounting /Volumes"; sudo mount  -t vboxsf Volumes /Volumes; }}' """.format(os.environ['DOCKER_MACHINE_NAME']),
                        shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    env.logger.trace('Sucessfully mount /Volumes to virtual machine')
                    self.has_volumes = True
                except Exception as e:
                    env.logger.trace('Failed to mount /Volumes to virtual machine: {}'.format(e))
        except Exception as e:
            env.logger.debug(e)
            self.client = None

    def total_memory(self, image='ubuntu'):
        '''Get the available ram fo the docker machine in Kb'''
        ret = subprocess.check_output(
            '''docker run -t {} cat /proc/meminfo  | grep MemTotal'''.format(image),
            shell=True, stdin=subprocess.DEVNULL)
        # ret: MemTotal:       30208916 kB
        self.tot_mem = int(ret.split()[1])
        return self.tot_mem

    def _is_image_avail(self, image):
        images = sum([x['RepoTags'] for x in self.client.images()], [])
        # some earlier version of docker-py returns docker.io/ for global repositories
        images = [x[10:] if x.startswith('docker.io/') else x for x in images]
        return (':' in image and image in images) or \
            (':' not in image and '{}:latest'.format(image) in images)

    def stream(self, line):
        # properly output streamed output
        try:
            sys.stdout.write(json.loads(line).get('stream', ''))
        except ValueError:
            # sometimes all the data is sent on a single line ????
            #
            # ValueError: Extra data: line 1 column 87 - line 1 column
            # 33268 (char 86 - 33267)
            # This ONLY works because every line is formatted as
            # {"stream": STRING}
            for obj in re.findall('{\s*"stream"\s*:\s*"[^"]*"\s*}', line):
                sys.stdout.write(json.loads(obj).get('stream', ''))

    def build(self, script, **kwargs):
        if not self.client:
            raise RuntimeError('Cannot connect to the Docker daemon. Is the docker daemon running on this host?')
        if script is not None:
            f = BytesIO(script.encode('utf-8'))
            for line in self.client.build(fileobj=f, **kwargs):
                self.stream(line.decode())
        else:
            for line in self.client.build(**kwargs):
                self.stream(line.decode())
        # if a tag is given, check if the image is built
        if 'tag' in kwargs and not self._is_image_avail(kwargs['tag']):
            raise RuntimeError('Image with tag {} is not created.'.format(kwargs['tag']))

    def import_image(self, image, **kwargs):
        if not self.client:
            raise RuntimeError('Cannot connect to the Docker daemon. Is the docker daemon running on this host?')
        env.logger.info('docker import {}'.format(image))
        self.client.import_image(image, **kwargs)

    def pull(self, image):
        if not self.client:
            raise RuntimeError('Cannot connect to the Docker daemon. Is the docker daemon running on this host?')
        # if image is specified, check if it is available locally. If not, pull it
        ret = 0
        if not self._is_image_avail(image):
            env.logger.info('docker pull {}'.format(image))
            # using subprocess instead of docker-py's pull function because this would have
            # much better progress bar display
            ret = subprocess.call('docker pull {}'.format(image), shell=True)
            #for line in self.client.pull(image, stream=True):
            #    self.stream(line)
        if not self._is_image_avail(image):
            raise RuntimeError('Failed to pull image {}'.format(image))
        return ret

    def commit(self, **kwargs):
        if not self.client:
            raise RuntimeError('Cannot connect to the Docker daemon. Is the docker daemon running on this host?')
        for line in self.client.commit(**kwargs):
            self.stream(line.decode())
        return 0

    def run(self, image, script='', interpreter='', suffix='.sh', **kwargs):
        if self.client is None:
            raise RuntimeError('Cannot connect to the Docker daemon. Is the docker daemon running on this host?')
        #
        env.logger.debug('docker_run with keyword args {}'.format(kwargs))
        #
        # now, write a temporary file to a tempoary directory under the current directory, this is because
        # we need to share the directory to ...
        with tempfile.TemporaryDirectory(dir=os.getcwd()) as tempdir:
            # keep the temporary script for debugging purposes
            # tempdir = tempfile.mkdtemp(dir=os.getcwd())
            if script:
                tempscript = 'docker_run_{}{}'.format(os.getpid(), suffix)
                with open(os.path.join(tempdir, tempscript), 'w') as script_file:
                    script_file.write(script)
            #
            binds = []
            if 'volumes' in kwargs:
                volumes = [kwargs['volumes']] if isinstance(kwargs['volumes'], str) else kwargs['volumes']
                for vol in volumes:
                    if not vol:
                        continue
                    if vol.count(':') != 1:
                        raise RuntimeError('Please specify columes in the format of host_dir:mnt_dir')
                    host_dir, mnt_dir = vol.split(':')
                    if platform.system() == 'Darwin':
                        # under Darwin, host_dir must be under /Users
                        if not os.path.abspath(host_dir).startswith('/Users') and not (self.has_volumes and os.path.abspath(host_dir).startswith('/Volumes')):
                            raise RuntimeError('hostdir ({}) under MacOSX must be under /Users or /Volumes (if properly configured, see https://github.com/bpeng2000/SOS/wiki/SoS-Docker-guide for details) to be usable in docker container'.format(host_dir))
                    binds.append('{}:{}'.format(os.path.abspath(host_dir), mnt_dir))
            #
            volumes_opt = ' '.join('-v {}'.format(x) for x in binds)
            # under mac, we by default share /Users within docker
            if platform.system() == 'Darwin':
                if not any(x.startswith('/Users:') for x in binds):
                    volumes_opt += ' -v /Users:/Users'
                if self.has_volumes:
                    volumes_opt += ' -v /Volumes:/Volumes'
            if not any(x.startswith('/tmp:') for x in binds):
                volumes_opt += ' -v /tmp:/tmp'
            #
            mem_limit_opt = ''
            if 'mem_limit' in kwargs:
                mem_limit_opt = '--memory={}'.format(kwargs['mem_limit'])
            #
            volumes_from_opt = ''
            if 'volumes_from' in kwargs:
                if isinstance(kwargs['volumes_from'], str):
                    volumes_from_opt = '--volumes_from={}'.format(kwargs['volumes_from'])
                elif isinstance(kwargs['volumes_from'], list):
                    volumes_from_opt = ' '.join('--volumes_from={}'.format(x) for x in kwargs['volumes_from'])
                else:
                    raise RuntimeError('Option volumes_from only accept a string or list of string'.format(kwargs['volumes_from']))
            # we also need to mount the script
            cmd_opt = ''
            if script and interpreter:
                volumes_opt += ' -v {}:{}'.format(os.path.join(tempdir, tempscript), '/var/lib/sos/{}'.format(tempscript))
                cmd_opt = interpreter.replace('{}', '/var/lib/sos/{}'.format(tempscript))
            #
            working_dir_opt = '-w={}'.format(os.path.abspath(os.getcwd()))
            if 'working_dir' in kwargs:
                if not os.path.isabs(kwargs['working_dir']):
                    env.logger.warning('An absolute path is needed for -w option of docker run command. "{}" provided, "{}" used.'
                        .format(kwargs['working_dir'], os.path.abspath(os.path.expanduser(kwargs['working_dir']))))
                    working_dir_opt = '-w={}'.format(os.path.abspath(os.path.expanduser(kwargs['working_dir'])))
                else:
                    working_dir_opt = '-w={}'.format(kwargs['working_dir'])
            #
            env_opt = ''
            if 'environment' in kwargs:
                if isinstance(kwargs['environment'], dict):
                    env_opt = ' '.join('-e {}={}'.format(x,y) for x,y in kwargs['environment'].items())
                elif isinstance(kwargs['environment'], list):
                    env_opt = ' '.join('-e {}'.format(x) for x in kwargs['environment'])
                elif isinstance(kwargs['environment'], str):
                    env_opt = '-e {}'.format(kwargs['environment'])
                else:
                    raise RuntimeError('Invalid value for option environment (str, list, or dict is allowd, {} provided)'.format(kwargs['environment']))
            #
            port_opt = '-P'
            if 'port' in kwargs:
                if isinstance(kwargs['port'], (str, int)):
                    port_opt = '-p {}'.format(kwargs['port'])
                elif isinstance(kwargs['port'], list):
                    port_opt = ' '.join('-p {}'.format(x) for x in kwargs['port'])
                else:
                    raise RuntimeError('Invalid value for option port (a list of intergers), {} provided'.format(kwargs['port']))
            #
            name_opt = ''
            if 'name' in kwargs:
                name_opt = '--name={}'.format(kwargs['name'])
            #
            stdin_opt = ''
            if 'stdin_open' in kwargs and kwargs['stdin_optn']:
                stdin_opt = '-i'
            #
            tty_opt = '-t'
            if 'tty' in kwargs and not kwargs['tty']:
                tty_opt = ''
            #
            user_opt = ''
            if 'user' in kwargs:
                user_opt = '-u {}'.format(kwargs['user'])
            #
            extra_opt = ''
            if 'extra_args' in kwargs:
                extra_opt = kwargs['extra_args']
            #
            security_opt = ''
            if platform.system() == 'Linux':
                # this is for a selinux problem when /var/sos/script cannot be executed
                security_opt = '--security-opt label:disable'
            command = 'docker run --rm {} {} {} {} {} {} {} {} {} {} {} {} {} {}'.format(
                security_opt,       # security option
                volumes_opt,        # volumes
                volumes_from_opt,   # volumes_from
                name_opt,           # name
                stdin_opt,          # stdin_optn
                tty_opt,            # tty
                port_opt,           # port
                working_dir_opt,    # working dir
                user_opt,           # user
                env_opt,            # environment
                mem_limit_opt,      # memory limit
                extra_opt,          # any extra parameters
                image,              # image
                cmd_opt
                )
            env.logger.info(command)
            ret = subprocess.call(command, shell=True)
            if ret != 0:
                msg = 'The script has been saved to .sos/{} so that you can execute it using the following command:\n{}'.format(
                    tempscript, command.replace(tempdir, os.path.abspath('./.sos')))
                shutil.copy(os.path.join(tempdir, tempscript), '.sos')
                if ret == 125:
                    raise RuntimeError('Docker daemon failed (exitcode=125). ' + msg)
                elif ret == 126:
                    raise RuntimeError('Failed to invoke specified command (exitcode=126). ' + msg)
                elif ret == 127:
                    raise RuntimeError('Failed to locate specified command (exitcode=127). ' + msg)
                elif ret == 137:
                    if not hasattr(self, 'tot_mem'):
                        self.tot_mem = self.total_memory(image)
                    raise RuntimeError('Script killed by docker, probably because of lack of RAM (available RAM={:.1f}GB, exitcode=137). '.format(self.tot_mem/1024/1024) + msg)
                else:
                    raise RuntimeError('Executing script in docker returns an error (exitcode={}). '.format(ret) + msg)
        return 0

#
# A decoration function that allows SoS to replace all SoS actions
# with a null action.
#
def SoS_Action(run_mode='run'):
    run_mode = [run_mode] if isinstance(run_mode, str) else run_mode
    def runtime_decorator(func):
        def action_wrapper(*args, **kwargs):
            runtime_options = env.sos_dict.get('_runtime', {})
            #
            non_runtime_options = {k:v for k,v in kwargs.items() if k not in SOS_RUNTIME_OPTIONS}
            # docker files will be downloaded in run or prepare mode
            if 'docker_file' in runtime_options and env.run_mode in ('run', 'prepare'):
                docker = DockerClient()
                docker.import_image(runtime_options['docker_file'])
            # handle image
            if 'docker_image' in runtime_options and env.run_mode in ('run', 'prepare'):
                docker = DockerClient()
                docker.pull(runtime_options['docker_image'])
                if env.run_mode == 'prepare':
                    mem = docker.total_memory(runtime_options['docker_image'])
                    if mem < 4000000: # < 4G
                        env.logger.warning('Docker machine has {:.1f} GB of total memory and might not be enough for your operation. Please refer to https://github.com/bpeng2000/SOS/wiki/SoS-Docker-guide to adjust the docker machine if needed.'
                            .format(mem/1024/1024))
                    else:
                        env.logger.debug('Docker machine has {:.1f} GB of total memory ram'.format(mem/1024/1024))
            if env.run_mode not in run_mode:
                # return dynamic expression when not in run mode, that is to say
                # the script logic cannot rely on the result of the action
                return Undetermined(func.__name__)
            return func(*args, **non_runtime_options)
        action_wrapper.run_mode = run_mode
        return action_wrapper
    return runtime_decorator


class SoS_ExecuteScript:
    def __init__(self, script, interpreter, suffix):
        self.script = script
        self.interpreter = interpreter
        self.suffix = suffix

    def run(self, **kwargs):
        if '{}' not in self.interpreter:
            self.interpreter += ' {}'
        runtime_options = env.sos_dict.get('_runtime', {})
        if 'docker_image' in runtime_options:
            docker = DockerClient()
            docker.run(runtime_options['docker_image'], self.script, self.interpreter, self.suffix,
                **kwargs)
        else:
            try:
                script_file = tempfile.NamedTemporaryFile(mode='w+t', suffix=self.suffix, delete=False).name
                with open(script_file, 'w') as sfile:
                    sfile.write(self.script)
                cmd = self.interpreter.replace('{}', shlex.quote(script_file))
                p = subprocess.Popen(cmd, shell=True)
                env.register_process(p.pid, 'Runing {}'.format(script_file))
                ret = p.wait()
            finally:
                env.deregister_process(p.pid)
                os.remove(script_file)
            if ret != 0:
                script_file = os.path.join('.sos/{}_{}{}'.format(self.interpreter.split()[0].split('/')[-1], os.getpid(), self.suffix))
                with open(script_file, 'w') as sfile:
                    sfile.write(self.script)
                cmd = self.interpreter.replace('{}', shlex.quote(script_file))
                raise RuntimeError('Failed to execute script. The script is saved to {}. Please use command "{}" to test it.'
                    .format(script_file, cmd))


@SoS_Action(run_mode=['dryrun', 'prepare', 'run'])
def sos_run(workflow, source={}):
    '''Execute a workflow from specified source, input, and output
    By default the workflow is defined in the existing SoS script, but
    extra sos files can be specified from paramter source. The workflow
    will be execute in the current step namespace with _input as workflow
    input. '''
    if env.run_mode == 'dryrun':
        env.logger.debug('Checking nested workflow {}'.format(workflow))
    elif env.run_mode == 'prepare':
        env.logger.debug('Preparing nested workflow {}'.format(workflow))
    else:
        env.logger.info('Executing nested workflow {}'.format(workflow))
    script = SoS_Script(env.sos_dict['__step_context__'].content, env.sos_dict['__step_context__'].filename)
    wf = script.workflow(workflow, source=source)
    # if wf contains the current step or one of the previous one, this constitute
    # recusive nested workflow and should not be allowed
    if env.sos_dict['step_name'] in ['{}_{}'.format(x.name, x.index) for x in wf.sections if not x.is_parameters]:
        raise RuntimeError('Nested workflow {} contains the current step {}'.format(workflow, env.sos_dict['step_name']))
    return Sequential_Executor(wf).run(args=env.sos_dict['__args__'], nested=True)

@SoS_Action(run_mode=['run'])
def execute_script(script, interpreter, suffix, **kwargs):
    return SoS_ExecuteScript(script, interpreter, suffix).run(**kwargs)

@SoS_Action(run_mode=['dryrun', 'run'])
def check_command(cmd, pattern = None):
    '''Raise an exception if output of `cmd` does not match specified `pattern`.
    Multiple patterns can be specified as a list of patterns.
    When pattern is None, check the existence of command `cmd`
    and raise an error if command does not exist.'''
    ret_val = 0
    if pattern is None and len(shlex.split(cmd)) == 1:
        name = shutil.which(cmd)
        if not name:
            raise RuntimeError('Command ``{}`` not found!'.format(cmd))
        env.logger.info('Command ``{}`` is located as ``{}``.'.format(cmd, name))
    else:
        try:
            output = subprocess.check_output(cmd, stderr=subprocess.STDOUT, shell=True, timeout=2).decode()
        except subprocess.TimeoutExpired as e:
            output = e.output
            ret_val = 1
            env.logger.warning(e)
            env.logger.warning(e.output.decode())
        except subprocess.CalledProcessError as e:
            ret_val = e.returncode
            output = e.output
            env.logger.warning(e)
            env.logger.warning(e.output.decode())
        #
        env.logger.trace('Output of command ``{}`` is ``{}``'.format(cmd, output))
        #
        if pattern:
            pattern = [pattern] if isinstance(pattern, str) else pattern
            if all([re.search(x, output, re.MULTILINE) is None for x in pattern]):
                raise RuntimeError('Output of command ``{}`` does not match specified regular expression ``{}``.'
                    .format(cmd, ' or '.join(pattern)))
    return ret_val

@SoS_Action(run_mode=['dryrun', 'run'])
def fail_if(expr, msg=''):
    '''Raise an exception with `msg` if condition `expr` is False'''
    if expr:
        raise RuntimeError(msg)
    return 0

@SoS_Action(run_mode=['dryrun', 'run'])
def warn_if(expr, msg=''):
    '''Yield an warning message `msg` if `expr` is False '''
    if expr:
        env.logger.warning(msg)
    return 0

#
# download file with progress bar
#
def downloadURL(URL, dest, decompress=False, index=None):
    # use libcurl
    dest = os.path.abspath(os.path.expanduser(dest))
    dest_dir, filename = os.path.split(dest)
    #
    if not os.path.isdir(dest_dir):
        os.makedirs(dest_dir)
    if not os.path.isdir(dest_dir):
        raise RuntimeError('Failed to create destination directory to download {}'.format(URL))
    #
    message = filename
    if len(message) > 30:
        message = message[:10] + '...' + message[-16:]
    #
    dest_tmp = dest + '.tmp_{}'.format(os.getpid())
    term_width = getTermWidth()
    try:
        env.logger.debug('Download {} to {}'.format(URL, dest))
        prog = ProgressBar(message, disp=env.verbosity > 1, index=index)
        sig = FileSignature(dest)
        if os.path.isfile(dest) and sig.validate():
            prog.done(message + ': \033[32m use existing {}\033[0m'.format(' '*(term_width - len(message) - 15)))
            return True
        #
        with open(dest_tmp, 'wb') as f:
            c = pycurl.Curl()
            c.setopt(pycurl.URL, str(URL))
            c.setopt(pycurl.WRITEFUNCTION, f.write)
            c.setopt(pycurl.SSL_VERIFYPEER, False)
            c.setopt(pycurl.NOPROGRESS, False)
            c.setopt(pycurl.PROGRESSFUNCTION, prog.curlUpdate)
            c.perform()
        if c.getinfo(pycurl.HTTP_CODE) == 404:
            prog.done(message + ':\033[91m 404 Error {}\033[0m'.format(' '*(term_width - len(message) - 12)))
            try:
                os.remove(dest_tmp)
            except OSError:
                pass
            return False
        os.rename(dest_tmp, dest)
        decompressed = 0
        if decompress:
            if zipfile.is_zipfile(dest):
                zip = zipfile.ZipFile(dest)
                zip.extractall(dest_dir)
                names = zip.namelist()
                for name in names:
                    if not os.path.isfile(os.path.join(dest_dir, name)):
                        return False
                    else:
                        sig.add(os.path.join(dest_dir, name))
                        decompressed += 1
            elif tarfile.is_tarfile(dest):
                with tarfile.open(dest, 'r:*') as tar:
                    tar.extractall(dest_dir)
                    # only extract files
                    files = [x.name for x in tar.getmembers() if x.isfile()]
                    for name in files:
                        if not os.path.isfile(os.path.join(dest_dir, name)):
                            return False
                        else:
                            sig.add(os.path.join(dest_dir, name))
                            decompressed += 1
            elif dest.endswith('.gz'):
                decomp = dest[:-3]
                with gzip.open(dest, 'rb') as fin, open(decomp, 'wb') as fout:
                    buffer = fin.read(100000)
                    while buffer:
                        fout.write(buffer)
                        buffer = fin.read(100000)
                sig.add(decomp)
                decompressed += 1
        decompress_msg = '' if not decompressed else ' ({} file{} decompressed)'.format(
            decompressed, '' if decompressed <= 1 else 's')
        prog.done(message + ':\033[32m downloaded{} {}\033[0m'.format(decompress_msg,
            ' '*(term_width - len(message) - 13 - len(decompress_msg))))
    except Exception as e:
        env.logger.error(e)
        return False
    finally:
        # if there is something wrong still remove temporary file
        if os.path.isfile(dest_tmp):
            os.remove(dest_tmp)
    sig.write()
    return os.path.isfile(dest)


@SoS_Action(run_mode=['prepare'])
def download(URLs, dest_dir='.', dest_file=None, decompress=False):
    '''Download files from specified URL, which should be space, tab or
    newline separated URLs. The files will be downloaded to specified
    destination. If `filename.md5` files are downloaded, they are used to
    validate downloaded `filename`. Unless otherwise specified, compressed
    files are decompressed.
    '''
    if isinstance(URLs, str):
        urls = [x.strip() for x in URLs.split() if x.strip()]
    else:
        urls = list(URLs)
    #
    if dest_file is not None and len(urls) != 1:
        raise RuntimeError('Only one URL is allowed if a destination file is specified.')
    #
    if dest_file is None:
        filenames = []
        for idx, url in enumerate(urls):
            token = urllib.parse.urlparse(url)
            # if no scheme or netloc, the URL is not acceptable
            if not all([getattr(token, qualifying_attr) for qualifying_attr in  ('scheme', 'netloc')]):
                filenames.append(None)
                continue
            filename = os.path.split(token.path)[-1]
            if not filename:
                filenames.append(None)
                continue
            filenames.append(os.path.join(dest_dir, filename))
    else:
        filenames = [dest_file]
    #
    succ = [False for x in urls]
    if len(succ) > 1:
        # first scroll several lines to reserve place for progress bar
        for url in urls:
            sys.stderr.write('\n')
        with mp.Pool(processes = env.sos_dict['CONFIG'].get('sos_download_processes', 5)) as pool:
            for idx, (url, filename) in enumerate(zip(urls, filenames)):
                if not filename:
                    continue
                succ[idx] = pool.apply_async(downloadURL, (url, filename,
                    decompress, len(urls) - idx))
            succ = [x.get() if isinstance(x, mp.pool.AsyncResult) else x for x in succ]
        #
        t = blessings.Terminal(stream=sys.stderr)
        sys.stderr.write(t.move( t.height, 0)) # + '\n')
    else:
        if dest_file is not None:
            succ[0] = downloadURL(urls[0], dest_file, decompress=decompress)
        else:
           if filenames[0]:
                succ[0] = downloadURL(urls[0], filenames[0], decompress=decompress)
    #
    for su, url in zip(succ, urls):
        if not su:
            env.logger.warning('Failed to download {}'.format(url))
    if not all(succ):
        raise RuntimeError('Not all files have been downloaded')
    # if downloaded files contains .md5 signature, use them to validate
    # downloaded files.
    for filename in [x for x in filenames if x.endswith('.md5')]:
        if filename[:-4] in filenames and os.path.isfile(filename[:-4]):
            with open(filename) as md5:
                rec_md5 = md5.readline().split()[0].strip()
                obs_md5 = fileMD5(filename[:-4], partial=False)
                if rec_md5 != obs_md5:
                    env.logger.warning('md5 signature mismatch for downloaded file {} (recorded {}, observed {})'
                        .format(filename[:-4], rec_md5, obs_md5))
                    return 1
    return 0

@SoS_Action(run_mode='run')
def run(script, **kwargs):
    return SoS_ExecuteScript(script, '/bin/bash', '.sh').run(**kwargs)

@SoS_Action(run_mode='run')
def bash(script, **kwargs):
    return SoS_ExecuteScript(script, '/bin/bash', '.sh').run(**kwargs)

@SoS_Action(run_mode='run')
def csh(script, **kwargs):
    return SoS_ExecuteScript(script, '/bin/csh', '.csh').run(**kwargs)

@SoS_Action(run_mode='run')
def tcsh(script, **kwargs):
    return SoS_ExecuteScript(script, '/bin/tcsh', '.sh').run(**kwargs)

@SoS_Action(run_mode='run')
def zsh(script, **kwargs):
    return SoS_ExecuteScript(script, '/bin/zsh', '.zsh').run(**kwargs)

@SoS_Action(run_mode='run')
def sh(script, **kwargs):
    return SoS_ExecuteScript(script, '/bin/sh', '.sh').run(**kwargs)

@SoS_Action(run_mode='run')
def python(script, **kwargs):
    return SoS_ExecuteScript(script, 'python', '.py').run(**kwargs)

@SoS_Action(run_mode='run')
def python3(script, **kwargs):
    return SoS_ExecuteScript(script, 'python3', '.py').run(**kwargs)

@SoS_Action(run_mode='run')
def perl(script, **kwargs):
    return SoS_ExecuteScript(script, 'perl', '.pl').run(**kwargs)

@SoS_Action(run_mode='run')
def ruby(script, **kwargs):
    return SoS_ExecuteScript(script, 'ruby', '.rb').run(**kwargs)

@SoS_Action(run_mode='run')
def node(script, **kwargs):
    return SoS_ExecuteScript(script, 'node', '.js').run(**kwargs)

@SoS_Action(run_mode='run')
def JavaScript(script, **kwargs):
    return SoS_ExecuteScript(script, 'node', '.js').run(**kwargs)

@SoS_Action(run_mode='run')
def R(script, **kwargs):
    return SoS_ExecuteScript(script, 'Rscript --default-packages='\
                             'methods,utils,stats,grDevices,graphics ', '.R').run(**kwargs)

@SoS_Action(run_mode=['prepare'])
def check_R_library(name, version = None):
    '''Check existence and version match of R library.
    cran and bioc packages are unique yet might overlap with github.
    Therefore if the input name is {repo}/{pkg} the package will be
    installed from github if not available, else from cran or bioc
    '''
    output_file = tempfile.NamedTemporaryFile(mode='w+t', suffix='.txt', delete=False).name
    if len(glob_wildcards('{repo}/{pkg}', [name])['repo']):
        # package is from github
        check_R_library('devtools')
        install_script = interpolate('''
        options(warn=-1)
        package_repo <- ${name!r}
        package <- basename(package_repo)
        if (require(package, character.only=TRUE, quietly=TRUE)) {
            write(paste(package, packageVersion(package), "AVAILABLE"), file="${output_file}")
        } else {
            devtools::install_github(package_repo)
            # if it still does not exist, write the package name to output
            if (require(package, character.only=TRUE, quietly=TRUE)) {
                write(paste(package, packageVersion(package), "INSTALLED"), file="${output_file}")
            } else {
                write(paste(package, "NA", "MISSING"), file="${output_file}")
                quit("no")
            }
        }
        cur_version <- packageVersion(package)
        ''', '${ }', locals())
    else:
        # package is from cran or bioc
        install_script = interpolate('''
        options(warn=-1)
        package <- ${name!r}
        if (require(package, character.only=TRUE, quietly=TRUE)) {
            write(paste(package, packageVersion(package), "AVAILABLE"), file="${output_file}")
        } else {
            install.packages(package, repos="http://cran.us.r-project.org",
                quiet=FALSE)
            # if the package still does not exist
            if (!require(package, character.only=TRUE, quietly=TRUE)) {
                source("http://bioconductor.org/biocLite.R")
                biocLite(package, suppressUpdates=TRUE, suppressAutoUpdate=TRUE, ask=FALSE)
            }
            # if it still does not exist, write the package name to output
            if (require(package, character.only=TRUE, quietly=TRUE)) {
                write(paste(package, packageVersion(package), "INSTALLED"), file="${output_file}")
            } else {
                write(paste(package, "NA", "MISSING"), file="${output_file}")
                quit("no")
            }
        }
        cur_version <- packageVersion(package)
        ''', '${ }', locals())
    version_script = ''
    if version is not None:
        version = [version] if isinstance(version, str) else version
        operators = []
        for idx, value in enumerate(version):
            value = str(value)
            if value.endswith('+'):
                operators.append('>=')
                version[idx] = value[:-1]
            elif value.endswith('-'):
                operators.append('<')
                version[idx] = value[:-1]
            else:
                operators.append('==')
        # check version and mark version mismatch
        # if current version satisfies any of the
        # requirement the check program quits
        for x, y in zip(version, operators):
            version_script += '''
            if (cur_version {1} {0}) {{
              quit("no")
            }}
            '''.format(repr(x), y)
        version_script += 'write(paste(package, cur_version, "VERSION_MISMATCH"), file = {})'.\
          format(repr(output_file))
    SoS_ExecuteScript(install_script + version_script, 'Rscript --default-packages=utils', '.R').run()
    ret_val = 0
    with open(output_file) as tmp:
        for line in tmp:
            lib, version, status = line.split()
            if status.strip() == "MISSING":
                raise RuntimeError('R Library {} is not available and cannot be installed.'.format(lib))
            elif status.strip() == 'AVAILABLE':
                env.logger.info('R library {} ({}) is available'.format(lib, version))
            elif status.strip() == 'INSTALLED':
                env.logger.info('R library {} ({}) has been installed'.format(lib, version))
            elif status.strip() == 'VERSION_MISMATCH':
                env.logger.warning('R library {} ({}) does not satisfy version requirement!'.format(lib, version))
                ret_val = 1
            else:
                raise RuntimeError('This should not happen: {}'.format(line))
    try:
        os.remove(output_file)
    except:
        pass
    return ret_val


@SoS_Action(run_mode='run')
def docker_build(dockerfile=None, **kwargs):
    '''docker build command. By default a script is sent to the docker build command but
    you can also specify different parameters defined inu//docker-py.readthedocs.org/en/stable/api/#build
    '''
    docker = DockerClient()
    docker.build(dockerfile, **kwargs)
    return 0


@SoS_Action(run_mode='run')
def docker_commit(**kwargs):
    docker = DockerClient()
    docker.commit(**kwargs)
    return 0

@SoS_Action(run_mode=['dryrun', 'run'])
def report(script, **kwargs):
    if 'filename' in kwargs:
        report_file = kwargs['filename']
    else:
        env.logger.trace('report {} to {}'.format(shortRepr(script), env.sos_dict['__step_report__']))
        report_file = env.sos_dict['__step_report__']
    # write report file (the ${} expressions must have been interpolated.
    with open(report_file, kwargs['mode'] if 'mode' in kwargs else 'a') as md:
        md.write(script)

def natural_keys(text):
    '''
    alist.sort(key=natural_keys) sorts in human order
    http://nedbatchelder.com/blog/200712/human_sorting.html
    (See Toothy's implementation in the comments)
    '''
    return [ int(c) if c.isdigit() else c for c in re.split('(\d+)', text) ]


@SoS_Action(run_mode=['dryrun', 'run'])
def pandoc(script=None, output=None, **kwargs):
    '''This action can be used in three ways

    pandoc:   outputfile='report.html'
      script

    pandoc(filename='report.sos', outputfile='report.html')

    pandoc(outputfile='report.html')

    '''
    # if in dryrun mode, check for pandoc command
    if env.run_mode == 'dryrun':
        return check_command('pandoc')
    #
    # in run mode, collect report and call pandoc
    sos_script = env.sos_dict['__step_context__'].filename
    # this is the case for stirng input (test only)
    if sos_script is None:
        sos_script = 'string_input'
    if script is not None:
        # get a temporary file with the content
        script_file = '{}.md'.format(os.path.basename(sos_script))
        with open(script_file, 'w') as report:
            report.write(script)
    elif 'filename' in kwargs:
        script_file = kwargs['filename']
    else:
        step_reports = glob.glob('.sos/report/*')
        step_reports.sort(key=natural_keys)
        # merge the files
        script_file = '{}.md'.format(os.path.basename(sos_script))
        env.logger.trace('Gathering reports {} to {}'.format(', '.join(step_reports), script_file))
        with open(script_file, 'w') as combined:
            for step_report in step_reports:
                with open(step_report, 'r') as md:
                    combined.write(md.read())
# $ pandoc -h
#                     
#     # this is output format
#     pandoc [OPTIONS] [FILES]
# Input formats:  commonmark, docbook, docx, epub, haddock, html, json*, latex,
#                 markdown, markdown_github, markdown_mmd, markdown_phpextra,
#                 markdown_strict, mediawiki, native, odt, opml, org, rst, t2t,
#                 textile, twiki
#                 [ *only Pandoc's JSON version of native AST]
# Output formats: asciidoc, beamer, commonmark, context, docbook, docx, dokuwiki,
#                 dzslides, epub, epub3, fb2, haddock, html, html5, icml, json*,
#                 latex, man, markdown, markdown_github, markdown_mmd,
#                 markdown_phpextra, markdown_strict, mediawiki, native, odt,
#                 opendocument, opml, org, pdf**, plain, revealjs, rst, rtf, s5,
#                 slideous, slidy, tei, texinfo, textile
#                 [**for pdf output, use latex or beamer and -o FILENAME.pdf]
# Options:
#   -f FORMAT, -r FORMAT  --from=FORMAT, --read=FORMAT                    
#   -t FORMAT, -w FORMAT  --to=FORMAT, --write=FORMAT                     
#   -o FILENAME           --output=FILENAME                               
#                         --data-dir=DIRECTORY                            
#   -R                    --parse-raw                                     
#   -S                    --smart                                         
#
# IGNORED
#
    arg_from = '--from={}'.format(kwargs['from']) if 'from' in kwargs else ''
    arg_to = '--to={}'.format(kwargs['to']) if 'to' in kwargs else ''
    if output is None:
        raise RuntimeError('Parameter output is required for action pandoc')
    elif not isinstance(output, str):
        raise RuntimeError('A filename is expected, {} provided'.format(output))
    arg_output = '--output={}'.format(shlex.quote(output))
    extra_args = ''
    if 'extra_args' in kwargs:
        ea = kwargs['extra_args']
        if isinstance(ea, str):
            extra_args = ea
        elif isintace(ea, Sequence):
            extra_args = ' '.join(list(ea))
        elif isinstance(ea, dict):
            extra_args = ' '.join('--{}={}'.format(k,v) for k,v in ea.items())
    #
    command = 'pandoc {} {} {} {} {{}}'.format(arg_from, arg_to, arg_output, extra_args)
    try:
        cmd = command.replace('{}', shlex.quote(script_file))
        env.logger.trace('Running command "{}"'.format(cmd))
        p = subprocess.Popen(cmd, shell=True)
        env.register_process(p.pid, 'Runing {}'.format(script_file))
        ret = p.wait()
    finally:
        env.deregister_process(p.pid)
        os.remove(script_file)
    if ret != 0:
        temp_file = os.path.join('.sos/{}_{}.md'.format(self.interpreter.split()[0], os.getpid()))
        shutil.copyfile(script_file, temp_file)
        cmd = command.replace('{}', shlex.quote(temp_file))
        raise RuntimeError('Failed to execute script. The script is saved to {}. Please use command "{}" to test it.'
            .format(temp_file, cmd))
    env.logger.info('Report saved to {}'.format(output))


@SoS_Action(run_mode=['dryrun', 'prepare', 'run'])
def Rmarkdown(script=None, output=None, **kwargs):
    '''This action can be used in three ways

    Rmarkdown:   outputfile='report.html'
      script

    Rmarkdown(filename='report.sos', outputfile='report.html')

    Rmarkdown(outputfile='report.html')

    '''
    # if in dryrun mode, check for Rmarkdown command
    if env.run_mode == 'dryrun':
        return check_command('Rscript')
    elif env.run_mode == 'prepare':
        return check_R_library('knitr')
    #
    # in run mode, collect report and call Rmarkdown
    sos_script = env.sos_dict['__step_context__'].filename
    # this is the case for stirng input (test only)
    if sos_script is None:
        sos_script = 'string_input'
    if script is not None:
        # get a temporary file with the content
        script_file = '{}.Rmd'.format(os.path.basename(sos_script))
        with open(script_file, 'w') as report:
            report.write(script)
    elif 'filename' in kwargs:
        script_file = kwargs['filename']
    else:
        step_reports = glob.glob('.sos/report/*')
        step_reports.sort(key=natural_keys)
        # merge the files
        script_file = '{}.Rmd'.format(os.path.basename(sos_script))
        env.logger.trace('Gathering reports {} to {}'.format(', '.join(step_reports), script_file))
        with open(script_file, 'w') as combined:
            for step_report in step_reports:
                with open(step_report, 'r') as md:
                    combined.write(md.read())
    # 
    arg_from = '--from={}'.format(kwargs['from']) if 'from' in kwargs else ''
    arg_to = '--to={}'.format(kwargs['to']) if 'to' in kwargs else ''
    if output is None:
        raise RuntimeError('Parameter output is required for action Rmarkdown')
    elif not isinstance(output, str):
        raise RuntimeError('A filename is expected, {} provided'.format(output))
    arg_output = '--output={}'.format(shlex.quote(output))
    extra_args = ''
    if 'extra_args' in kwargs:
        ea = kwargs['extra_args']
        if isinstance(ea, str):
            extra_args = ea
        elif isintace(ea, Sequence):
            extra_args = ' '.join(list(ea))
        elif isinstance(ea, dict):
            extra_args = ' '.join('--{}={}'.format(k,v) for k,v in ea.items())
    #
    command = 'Rmarkdown {} {} {} {} {{}}'.format(arg_from, arg_to, arg_output, extra_args)
    try:
        cmd = command.replace('{}', shlex.quote(script_file))
        p = subprocess.Popen(cmd, shell=True)
        env.register_process(p.pid, 'Runing {}'.format(script_file))
        ret = p.wait()
    finally:
        env.deregister_process(p.pid)
        os.remove(script_file)
    if ret != 0:
        temp_file = os.path.join('.sos/{}_{}.Rmd'.format(self.interpreter.split()[0], os.getpid()))
        shutil.copyfile(script_file, temp_file)
        cmd = command.replace('{}', shlex.quote(temp_file))
        raise RuntimeError('Failed to execute script. The script is saved to {}. Please use command "{}" to test it.'
            .format(temp_file, cmd))
    env.logger.info('Report saved to {}'.format(output))
