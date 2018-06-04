#!/usr/bin/env python3
#
# Copyright (c) Bo Peng and the University of Texas MD Anderson Cancer Center
# Distributed under the terms of the 3-clause BSD License.

import os
import time
from collections import defaultdict
from contextlib import contextmanager

from .utils import TimeoutInterProcessLock, env, format_HHMMSS


@contextmanager
def workflow_report(mode='a'):
    if '__workflow_sig__' not in env.sos_dict:
        with open(os.devnull, "w") as sig:
            yield sig
    else:
        workflow_sig = env.sos_dict['__workflow_sig__']
        with TimeoutInterProcessLock(workflow_sig + '_'):
            with open(workflow_sig, mode) as sig:
                yield sig


class Report(object):
    def __init__(self, workflow_info):
        self.data = defaultdict(lambda: defaultdict(list))
        with open(workflow_info, 'r') as wi:
            for line in wi:
                try:
                    entry_type, id, item = line.split('\t', 2)
                    self.data[entry_type][id].append(item.strip())
                except Exception as e:
                    env.logger.debug(f'Failed to read report line {line}: {e}')

    def master_id(self):
        try:
            return list(self.data['workflow_subworkflows'].keys())[0]
        except Exception as e:
            env.logger.warning(f'Failed to obtain master id: {e}')
            return 'NA'

    def workflow_name(self, id):
        try:
            return self.data['workflow_name'][id][0]
        except Exception as e:
            env.logger.warning(f'Failed to obtain workflow_name for {id}: {e}')
            return 'NA'

    def workflow_start_time(self, id):
        try:
            return time.strftime('%Y-%m-%d %H:%M:%S',
                                 time.localtime(float(self.data['workflow_start_time'][id][0])))
        except Exception as e:
            env.logger.warning(f'Failed to obtain workflow_start_time {id}: {e}')
            return 'NA'

    def workflow_end_time(self, id):
        try:
            return time.strftime('%Y-%m-%d %H:%M:%S',
                                 time.localtime(float(self.data['workflow_end_time'][id][0])))
        except Exception as e:
            env.logger.warning(f'Failed to obtain workflow_end_time {id}: {e}')
            return 'NA'

    def workflow_duration(self, id):
        try:
            return format_HHMMSS(int(float(self.data['workflow_end_time'][id][0])
                                     - float(self.data['workflow_start_time'][id][0])))
        except Exception as e:
            env.logger.warning(f'Failed to obtain workflow duration {id}: {e}')
            return 'NA'

    def workflow_stat(self, id):
        try:
            return eval(self.data['workflow_stat'][id][0])
        except:
            # env.logger.warning(f'Failed to obtain workflow_stat {id} {key}: {e}')
            return '0'

    def workflow_subworkflows(self, id):
        try:
            return self.data['workflow_subworkflows'][id]
        except Exception as e:
            env.logger.warning(f'Failed to obtain workflow_subworkflows {id}: {e}')
            return 'NA'

    def workflow_command_line(self, id):
        try:
            return self.data['workflow_command_line'][id][0]
        except Exception as e:
            env.logger.warning(f'Failed to obtain command line {id}: {e}')
            return 'NA'

    def tasks(self):
        try:
            return {id: eval(res[0]) for id, res in self.data['task'].items()}
        except:
            return {}


def render_report(output_file, workflow_info):
    data = Report(workflow_info)
    id = data.master_id()

    from jinja2 import Environment, PackageLoader, select_autoescape
    template = Environment(
        loader=PackageLoader('sos', 'templates'),
        autoescape=select_autoescape(['html', 'xml'])
    ).get_template('workflow_report.tpl')
    with open(output_file, 'w') as wo:
        wo.write(template.render({
            'workflow_name': data.workflow_name(id),
            'workflow_start_time': data.workflow_start_time(id),
            'workflow_end_time': data.workflow_end_time(id),
            'workflow_duration': data.workflow_duration(id),
            'stat': data.workflow_stat(id),
            'tasks': data.tasks(),
        }))
    env.logger.info(f'Summary of workflow saved to {output_file}')