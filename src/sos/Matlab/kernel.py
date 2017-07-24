#!/usr/bin/env python3
#
# This file is part of Script of Scripts (sos), a workflow system
# for the execution of commands and scripts in different languages.
# Please visit https://github.com/vatlab/SOS for more information.
#
# Copyright (C) 2016 Bo Peng (bpeng@mdanderson.org)
#
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
import pandas as pd
import numpy as np
import scipy.io as sio
import os
from collections import Sequence
import tempfile
from sos.utils import short_repr, env
from IPython.core.error import UsageError

def homogeneous_type(seq):
    iseq = iter(seq)
    first_type = type(next(iseq))
    if first_type in (int, float):
        return True if all(isinstance(x, (int, float)) for x in iseq) else False
    else:
        return True if all(isinstance(x, first_type) for x in iseq) else False

#
#  support for %get
#
#  Converting a Python object to a Matlab expression that will be executed
#  by the Matlab kernel.
#
#
def _Matlab_repr(obj):
    if isinstance(obj, bool):
        return 'true' if obj else 'false'
    elif isinstance(obj, (int, float, str, complex)):
        return repr(obj)
    elif isinstance(obj, Sequence):
        if len(obj) == 0:
            return '[]'
        
        # if the data is of homogeneous type, let us use []
        
        if homogeneous_type(obj):
            return '[' + ','.join(_Matlab_repr(x) for x in obj) + ']'
        else:
            return '{' + ','.join(_Matlab_repr(x) for x in obj) + '}'
    elif obj is None:
        return 'NaN'
    elif isinstance(obj, dict):
        return 'struct(' + ','.join('{},{}'.format(_Matlab_repr(x),
                                                   _Matlab_repr(y)) for (x, y) in
                                    obj.items()) + ')'


    elif isinstance(obj, set):
        return '{' + ','.join(_Matlab_repr(x) for x in obj) + '}'
    elif isinstance(obj, (
                          np.intc,
                          np.intp,
                          np.int8,
                          np.int16,
                          np.int32,
                          np.int64,
                          np.uint8,
                          np.uint16,
                          np.uint32,
                          np.uint64,
                          np.float16,
                          np.float32,
                          np.float64,
                          )):
        return repr(obj)

    elif isinstance(obj, np.matrixlib.defmatrix.matrix):
        dic = tempfile.tempdir
        os.chdir(dic)
        sio.savemat('mat2mtlb.mat', {'obj': obj})
        return 'cell2mat(struct2cell(load(fullfile(' + '\'' + dic + '\'' + ',' \
            + '\'mat2mtlb.mat\'))))'
    elif isinstance(obj, np.ndarray):
        dic = tempfile.tempdir
        os.chdir(dic)
        sio.savemat('ary2mtlb.mat', {'obj': obj})
        return 'load(fullfile(' + '\'' + dic + '\'' + ',' \
            + '\'ary2mtlb.mat\'))'
    elif isinstance(obj, dict):
        dic = tempfile.tempdir
        os.chdir(dic)
        sio.savemat('dict2mtlb.mat', {'obj': obj})
        return 'load(fullfile(' + '\'' + dic + '\'' + ',' \
            + '\'dict2mtlb.mat\'))'
    elif isinstance(obj, pd.DataFrame):
        dic = tempfile.tempdir
        os.chdir(dic)
        obj.to_csv('df2mtlb.csv', index=False)
        return 'readtable(' + '\'' + dic + '/' + 'df2mtlb.csv\')'

Matlab_init_statements = r'''
path(path, {!r})
'''.format(os.path.split(__file__)[0])
print(Matlab_init_statements)

class sos_Matlab:
    supported_kernels = ['matlab', 'imatlab', 'octave']
    background_color = '#FA8072'
    options = {}

    def __init__(self, sos_kernel, kernel_name='matlab'):
        self.sos_kernel = sos_kernel
        self.kernel_name = kernel_name
        self.init_statements = Matlab_init_statements

    def get_vars(self, names):
        for name in names:
        #    if name.startswith('_'):
        #        self.sos_kernel.warn('Variable {} is passed from SoS to kernel {} as {}'.format(name, self.kernel_name, '.' + name[1:]))
        #        newname = '.' + name[1:]
        #    else:
        #        newname = name
            matlab_repr = _Matlab_repr(env.sos_dict[name])
            self.sos_kernel.run_cell('{} = {}'.format(name, matlab_repr), True, False,
                    on_error='Failed to get variable {} of type {} to Matlab'.format(name, env.sos_dict[name].__class__.__name__))

    def put_vars(self, items, to_kernel=None):
        # first let us get all variables with names starting with sos
        response = self.sos_kernel.get_response("display(cell2mat(who('sos*')))", ('stream',), name=('stdout',), debug=True)[0][1]
        all_vars = response['text'].strip().split()
        # in case there is no var with name starts with sos, the response would be
        #      []\n\n\n
        items += [x for x in all_vars if x != '[]']

        if not items:
            return {}

        result = {}
        for item in items:
            py_repr = 'display(sos_py_repr({}))'.format(item)
            response = self.sos_kernel.get_response(py_repr, ('stream',), name=('stdout',), debug=True)[0][1]
            expr = response['text']

            try:
                if 'loadmat' in expr:
                    # imported to be used by eval
                    from scipy.io import loadmat
                # evaluate as raw string to correctly handle \\ etc
                result[item] = eval(expr)
            except Exception as e:
                self.sos_kernel.warn('Failed to evaluate {!r}: {}'.format(expr, e))
        return result

    def sessioninfo(self):
        return self.sos_kernel.get_response(r'ver', ('stream',), name=('stdout',))[0][1]['text']
