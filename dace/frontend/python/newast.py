import ast
from collections import OrderedDict, namedtuple
import copy
import re
from typing import Any, Dict, List, Tuple, Union, Callable

import dace
from dace import data, dtypes, subsets, symbolic
from dace.config import Config
from dace.frontend.common import blas, op_impl
from dace.frontend.common import op_repository as oprepo
from dace.frontend.python import astutils
from dace.frontend.python.astutils import ExtNodeVisitor, ExtNodeTransformer, rname
from dace.graph import nodes
from dace.graph.labeling import propagate_memlet
from dace.memlet import Memlet
from dace.properties import LambdaProperty
from dace.sdfg import SDFG, SDFGState
from dace.symbolic import pystr_to_symbolic

import numpy as np
import sympy

# A type that defines assignment information
AssignmentInfo = Tuple[SDFG, SDFGState, Tuple[str]]


class ProgramVisitor:
    pass


@oprepo.replaces('dace.define_local')
@oprepo.replaces('numpy.ndarray')
def _define_local(sdfg: SDFG, state: SDFGState, shape, dtype):
    name, _ = sdfg.add_temp_transient(shape, dtype)
    return name


@oprepo.replaces('dace.define_stream')
def _define_stream(sdfg: SDFG,
                   state: SDFGState,
                   dtype=dtypes.float32,
                   buffer_size=1):
    """ Defines a local stream array in a DaCe program. """
    name = sdfg.temp_data_name()
    sdfg.add_stream(name, dtype, buffer_size=buffer_size, transient=True)
    return name


@oprepo.replaces('dace.define_streamarray')
@oprepo.replaces('dace.stream')
def _define_streamarray(sdfg: SDFG,
                        state: SDFGState,
                        dimensions,
                        dtype=dtypes.float32,
                        buffer_size=1):
    """ Defines a local stream array in a DaCe program. """
    name = sdfg.temp_data_name()
    sdfg.add_stream(
        name, dtype, shape=dimensions, buffer_size=buffer_size, transient=True)
    return name


def _assignop(sdfg: SDFG, state: SDFGState, op1: str, opcode: str,
              opname: str):
    """ Implements a general element-wise assignment operator. """
    arr1 = sdfg.arrays[op1]

    name, _ = sdfg.add_temp_transient(arr1.shape, arr1.dtype, arr1.storage)
    write_memlet = None
    if opcode:
        Memlet.simple(
            name,
            ','.join(['__i%d' % i for i in range(len(arr1.shape))]),
            wcr_str='lambda x, y: x %s y' % opcode)
    else:
        Memlet.simple(name,
                      ','.join(['__i%d' % i for i in range(len(arr1.shape))]))
    state.add_mapped_tasklet(
        "_%s_" % opname,
        {'__i%d' % i: '0:%s' % s
         for i, s in enumerate(arr1.shape)}, {
             'in1':
             Memlet.simple(
                 op1, ','.join(['__i%d' % i for i in range(len(arr1.shape))]))
         },
        'out = in1', {'out': write_memlet},
        external_edges=True)
    return name


def _unop(sdfg: SDFG, state: SDFGState, op1: str, opcode: str, opname: str):
    """ Implements a general element-wise unary operator. """
    arr1 = sdfg.arrays[op1]

    name, _ = sdfg.add_temp_transient(arr1.shape, arr1.dtype, arr1.storage)
    state.add_mapped_tasklet(
        "_%s_" % opname,
        {'__i%d' % i: '0:%s' % s
         for i, s in enumerate(arr1.shape)}, {
             'in1':
             Memlet.simple(
                 op1, ','.join(['__i%d' % i for i in range(len(arr1.shape))]))
         },
        'out = %s in1' % opcode, {
            'out':
            Memlet.simple(
                name, ','.join(['__i%d' % i for i in range(len(arr1.shape))]))
        },
        external_edges=True)
    return name


def _binop(sdfg: SDFG, state: SDFGState, op1: str, op2: str, opcode: str,
           opname: str, restype: dtypes.typeclass):
    """ Implements a general element-wise binary operator. """
    arr1 = sdfg.arrays[op1]
    arr2 = sdfg.arrays[op2]
    if (len(arr1.shape) != len(arr2.shape)
            or any(s1 != s2 for s1, s2 in zip(arr1.shape, arr2.shape))):
        raise SyntaxError('Array sizes must match')

    name, _ = sdfg.add_temp_transient(arr1.shape, restype, arr1.storage)
    state.add_mapped_tasklet(
        "_%s_" % opname,
        {'__i%d' % i: '0:%s' % s
         for i, s in enumerate(arr1.shape)}, {
             'in1':
             Memlet.simple(
                 op1, ','.join(['__i%d' % i for i in range(len(arr1.shape))])),
             'in2':
             Memlet.simple(
                 op2, ','.join(['__i%d' % i for i in range(len(arr1.shape))]))
         },
        'out = in1 %s in2' % opcode, {
            'out':
            Memlet.simple(
                name, ','.join(['__i%d' % i for i in range(len(arr1.shape))]))
        },
        external_edges=True)
    return name


def _scalarbinop(sdfg: SDFG,
                 state: SDFGState,
                 scalop: str,
                 arrop: str,
                 opcode: str,
                 opname: str,
                 restype: dtypes.typeclass,
                 reverse=False):
    """ Implements a general Scalar-Array binary operator. """
    scalar = sdfg.arrays[scalop]
    arr = sdfg.arrays[arrop]

    # Argument type checking
    if scalar.dtype != arr.dtype:
        print(
            "WARNING: Different types in scalar-array binary op (%s scalar, %s array)"
            % (scalar.dtype.to_string(), arr.dtype.to_string()))
    # End of argument type checking

    name, _ = sdfg.add_temp_transient(arr.shape, restype, arr.storage)
    state.add_mapped_tasklet(
        "_SA%s_" % opname,
        {'__i%d' % i: '0:%s' % s
         for i, s in enumerate(arr.shape)}, {
             'in1':
             Memlet.simple(scalop, '0'),
             'in2':
             Memlet.simple(
                 arrop, ','.join(['__i%d' % i
                                  for i in range(len(arr.shape))])),
         },
        'out = %s * %s' % ('in1' if reverse else 'in2', 'in2'
                           if reverse else 'in1'),
        {
            'out':
            Memlet.simple(
                name, ','.join(['__i%d' % i for i in range(len(arr.shape))]))
        },
        external_edges=True)
    return name


# Defined as a function in order to include the op and the opcode in the closure
def _makeassignop(op, opcode):
    @oprepo.replaces_operator('Array', op)
    def _op(visitor: ProgramVisitor, sdfg: SDFG, state: SDFGState, op1: str,
            op2: None):
        return _assignop(sdfg, state, op1, opcode, op)


def _makeunop(op, opcode):
    @oprepo.replaces_operator('Array', op)
    def _op(visitor: ProgramVisitor,
            sdfg: SDFG,
            state: SDFGState,
            op1: str,
            op2=None):
        return _unop(sdfg, state, op1, opcode, op)


def _is_scalar(sdfg, op):
    shape = sdfg.arrays[op].shape
    if len(shape) == 1 and shape[0] == 1:
        return True
    return False


def _inverse_dict_lookup(dict, value):
    for k, v in dict.items():
        if v == value:
            return k
    return None


def _makebinop(op, opcode):
    @oprepo.replaces_operator('Array', op)
    def _op(visitor: ProgramVisitor, sdfg: SDFG, state: SDFGState, op1: str,
            op2: str):

        arr1 = sdfg.arrays[op1]
        type1 = arr1.dtype.type
        isscal1 = _is_scalar(sdfg, op1)
        isnum1 = isscal1 and (op1 in visitor.numbers.values())
        if isnum1:
            type1 = _inverse_dict_lookup(visitor.numbers, op1)
        arr2 = sdfg.arrays[op2]
        type2 = arr2.dtype.type
        isscal2 = _is_scalar(sdfg, op2)
        isnum2 = isscal2 and (op2 in visitor.numbers.values())
        if isnum2:
            type2 = _inverse_dict_lookup(visitor.numbers, op2)
        restype = dace.DTYPE_TO_TYPECLASS[np.result_type(type1, type2).type]

        if isscal1:
            if isscal2:
                arr1 = sdfg.arrays[op1]
                arr2 = sdfg.arrays[op2]
                op3, arr3 = sdfg.add_temp_transient([1], restype, arr2.storage)
                tasklet = state.add_tasklet('_SS%s_' % op, {'s1', 's2'},
                                            {'s3'}, 's3 = s1 %s s2' % opcode)
                n1 = state.add_read(op1)
                n2 = state.add_read(op2)
                n3 = state.add_write(op3)
                state.add_edge(n1, None, tasklet, 's1',
                               dace.Memlet.from_array(op1, arr1))
                state.add_edge(n2, None, tasklet, 's2',
                               dace.Memlet.from_array(op2, arr2))
                state.add_edge(tasklet, 's3', n3, None,
                               dace.Memlet.from_array(op3, arr3))
                return op3
            else:
                return _scalarbinop(sdfg, state, op1, op2, opcode, op, restype)
        else:
            if isscal2:
                return _scalarbinop(sdfg, state, op2, op1, opcode, op, restype,
                                    True)
            else:
                return _binop(sdfg, state, op1, op2, opcode, op, restype)


# Define all standard Python augmented assignment operators
for op, opcode in [
    ('None', None),
    ('Add', '+'),
    ('Sub', '-'),
    ('Mult', '*'),
    ('Div', '/'),
    ('FloorDiv', '//'),
    ('Mod', '%'),
    ('Pow', '**'),
    ('LShift', '<<'),
    ('RShift', '>>'),
    ('BitOr', '|'),
    ('BitXor', '^'),
    ('BitAnd', '&'),
]:
    _makeassignop(op, opcode)

augassign_ops = {
    'Add': '+',
    'Sub': '-',
    'Mult': '*',
    'Div': '/',
    'FloorDiv': '//',
    'Mod': '%',
    'Pow': '**',
    'LShift': '<<',
    'RShift': '>>',
    'BitOr': '|',
    'BitXor': '^',
    'BitAnd': '&'
}

# Define all standard Python unary operators
for op, opcode in [('UAdd', '+'), ('USub', '-'), ('Not', '!'), ('Invert',
                                                                '~')]:
    _makeunop(op, opcode)

# Define all standard Python binary operators
# NOTE: ('MatMult', '@') is defined separately
for op, opcode in [('Add', '+'), ('Sub', '-'), ('Mult', '*'), ('Div', '/'),
                   ('FloorDiv', '//'), ('Mod', '%'), ('Pow', '**'), ('LShift',
                                                                     '<<'),
                   ('RShift', '>>'), ('BitOr', '|'), ('BitXor',
                                                      '^'), ('BitAnd', '&'),
                   ('And', '&&'), ('Or', '||'), ('Eq', '=='), ('NotEq', '!='),
                   ('Lt', '<'), ('LtE', '<='), ('Gt', '>'), ('GtE', '>=')]:
    _makebinop(op, opcode)


@oprepo.replaces_operator('Scalar', 'Mult', otherclass='Array')
def _scalarrmult(
        visitor,  #: ProgramVisitor,
        sdfg: SDFG,
        state: SDFGState,
        scalop: str,
        arrop: str,
        reverse=False):
    """ Implements a Scalar-Array element-wise multiplication operator. """
    scalar = scalop
    arr = sdfg.arrays[arrop]

    # Argument type checking
    if isinstance(scalar, str):
        if scalar not in sdfg.symbols:
            raise SyntaxError('Scalar "%s" not defined in SDFG' % scalar)
        scaltype = sdfg.symbols[scalar]
    else:
        scaltype = type(scalar)
        if scaltype in dtypes.DTYPE_TO_TYPECLASS:
            scaltype = dtypes.DTYPE_TO_TYPECLASS[scaltype]
    if scaltype != arr.dtype:
        print(
            "WARNING: Different types in scalar-array multiplication (%s scalar, %s array)"
            % (scaltype.to_string(), arr.dtype.to_string()))
    # End of argument type checking

    name, _ = sdfg.add_temp_transient(arr.shape, arr.dtype, arr.storage)
    state.add_mapped_tasklet(
        "_SA%s_" % 'Mult',
        {'__i%d' % i: '0:%s' % s
         for i, s in enumerate(arr.shape)}, {
             'inp':
             Memlet.simple(
                 arrop, ','.join(['__i%d' % i
                                  for i in range(len(arr.shape))])),
         },
        'out = %s * %s' % ('inp' if reverse else str(scalar), str(scalar)
                           if reverse else 'inp'),
        {
            'out':
            Memlet.simple(
                name, ','.join(['__i%d' % i for i in range(len(arr.shape))]))
        },
        external_edges=True)
    return name


@oprepo.replaces_operator('Array', 'Mult', otherclass='Scalar')
def _arrscalmult(visitor, sdfg: SDFG, state: SDFGState, op1: str, op2: str):
    return _scalarrmult(sdfg, state, op2, op1, reverse=True)


@oprepo.replaces_operator('Array', 'MatMult')
def _matmult(visitor, sdfg: SDFG, state: SDFGState, op1: str, op2: str):

    arr1 = sdfg.arrays[op1]
    arr2 = sdfg.arrays[op2]
    if (len(arr1.shape) != 2 or len(arr2.shape) != 2
            or arr1.shape[1] != arr2.shape[0]):
        raise SyntaxError('Matrix sizes must match')

    op3, arr3 = sdfg.add_temp_transient((arr1.shape[0], arr2.shape[1]),
                                        arr1.dtype, arr1.storage)

    state.add_mapped_tasklet(
        '_MatMult_', {
            '__i%d' % i: '0:%s' % s
            for i, s in enumerate(
                [arr1.shape[0], arr2.shape[1], arr1.shape[1]])
        }, {
            'a': Memlet.simple(op1, '__i0, __i2'),
            'b': Memlet.simple(op2, '__i2, __i1')
        },
        'c = a * b', {
            'c':
            Memlet.simple(
                op3,
                '__i0, __i1',
                wcr_str='lambda x, y: x + y',
                wcr_identity=0)
        },
        external_edges=True)

    # readA = state.add_read(op1)
    # readB = state.add_read(op2)
    # writeC = state.add_write(op3)

    # tasklet = blas.MatMulNode(inputs={'A', 'B'}, outputs={'C'})
    # state.add_node(tasklet)

    # state.add_edge(readA, None, tasklet, 'A', dace.Memlet.from_array(op1, arr1))
    # state.add_edge(readB, None, tasklet, 'B', dace.Memlet.from_array(op2, arr2))
    # state.add_edge(tasklet, 'C', writeC, None, dace.Memlet.from_array(op3, arr3))

    return op3


# @oprepo.replaces('dace.define_stream')
# def _define_stream(sdfg: SDFG, state: SDFGState, dtype=dtypes.float32, buffer_size=0):
#     name, _ = sdfg.add_temp_transient(shape, dtype)
#     return name
#
# @oprepo.replaces('dace.define_streamarray')
# def _define_streamarray(sdfg: SDFG, state: SDFGState, dimensions, dtype=dtypes.float32, buffer_size=0):
#     name, _ = sdfg.add_temp_transient(shape, dtype)
#     return name


def until(val, substr):
    """ Helper function that returns the substring of a string until a certain pattern. """
    if substr not in val:
        return val
    return val[:val.find(substr)]


@oprepo.replaces('dace.reduce')
def _reduce(sdfg: SDFG,
            state: SDFGState,
            redfunction: Callable[[Any, Any], Any],
            input: str,
            output=None,
            axis=None,
            identity=None):
    # TODO(later): If output is None, derive the output size from the input and create a new node
    if output is None:
        raise NotImplementedError
    inarr = until(input, '[')
    outarr = until(output, '[')

    # Convert axes to tuple
    if axis is not None and not isinstance(axis, (tuple, list)):
        axis = (axis, )
    if axis is not None:
        axis = tuple(pystr_to_symbolic(a) for a in axis)

    # Compute memlets
    input_subset = _parse_memlet_subset(sdfg.arrays[inarr],
                                        ast.parse(input).body[0].value, {})
    input_memlet = Memlet(inarr, input_subset.num_elements(), input_subset, 1)
    output_subset = _parse_memlet_subset(sdfg.arrays[outarr],
                                         ast.parse(output).body[0].value, {})
    output_memlet = Memlet(outarr, output_subset.num_elements(), output_subset,
                           1)

    # Create reduce subgraph
    inpnode = state.add_read(inarr)
    rednode = state.add_reduce(redfunction, axis, identity)
    outnode = state.add_write(outarr)
    state.add_nedge(inpnode, rednode, input_memlet)
    state.add_nedge(rednode, outnode, output_memlet)

    return []


############################################


def parse_dace_program(f, argtypes, global_vars, modules, other_sdfgs,
                       constants):
    """ Parses a `@dace.program` function into a _ProgramNode object.
        @param f: A Python function to parse.
        @param argtypes: An dictionary of (name, type) for the given
                         function's arguments, which may pertain to data
                         nodes or symbols (scalars).
        @param global_vars: A dictionary of global variables in the closure
                            of `f`.
        @param modules: A dictionary from an imported module name to the
                        module itself.
        @param other_sdfgs: Other SDFG and DaceProgram objects in the context
                            of this function.
        @param constants: A dictionary from a name to a constant value.
        @return: Hierarchical tree of `astnodes._Node` objects, where the top
                 level node is an `astnodes._ProgramNode`.
        @rtype: SDFG
    """
    src_ast, src_file, src_line, src = astutils.function_to_ast(f)

    # Resolve symbols to their names
    symrepl = {
        k: v.name
        for k, v in global_vars.items() if isinstance(v, symbolic.symbol)
    }
    src_ast = astutils.ASTFindReplace(symrepl).visit(src_ast)

    # Resolve data structures
    src_ast = StructTransformer(global_vars).visit(src_ast)

    src_ast = ModuleResolver(modules).visit(src_ast)
    # Convert modules after resolution
    for mod, modval in modules.items():
        if mod == 'builtins':
            continue
        newmod = global_vars[mod]
        del global_vars[mod]
        global_vars[modval] = newmod

    # Resolve constants to their values (if they are not already defined in this scope)
    src_ast = GlobalResolver({
        k: v
        for k, v in global_vars.items()
        if dtypes.isconstant(v) and not k in argtypes
    }).visit(src_ast)

    pv = ProgramVisitor(f.__name__, src_file, src_line, argtypes, global_vars,
                        {}, constants, other_sdfgs)

    sdfg, _, _ = pv.parse_program(src_ast.body[0])
    sdfg.set_sourcecode(src, 'python')

    return sdfg


class DaceSyntaxError(Exception):
    def __init__(self, visitor, node: ast.AST, message: str):
        self.visitor = visitor
        self.node = node
        self.message = message

    def __str__(self):
        # Try to recover line and column
        try:
            line = self.node.lineno
            col = self.node.col_offset
        except AttributeError:
            line = 0
            col = 0

        return (self.message + "\n  in File " + str(self.visitor.filename) +
                ", line " + str(line) + ":" + str(col))


class StructTransformer(ast.NodeTransformer):
    """ A Python AST transformer that replaces `Call`s to create structs with
        the custom StructInitializer AST node. """

    def __init__(self, gvars):
        super().__init__()
        self._structs = {
            k: v
            for k, v in gvars.items() if isinstance(v, dtypes.struct)
        }

    def visit_Call(self, node: ast.Call):
        # Struct initializer
        name = rname(node.func)
        if name not in self._structs:
            return self.generic_visit(node)

        # Parse name and fields
        struct = self._structs[name]
        name = struct.name
        fields = {rname(arg.arg): arg.value for arg in node.keywords}
        if tuple(fields.keys()) != tuple(struct.fields.keys()):
            raise SyntaxError('Mismatch in fields in struct definition')

        # Create custom node
        #new_node = astutils.StructInitializer(name, fields)
        #return ast.copy_location(new_node, node)

        node.func = ast.copy_location(
            ast.Name(id='__DAPPSTRUCT_' + name, ctx=ast.Load()), node.func)

        return node


# Replaces instances of modules Y imported with "import X as Y" by X
class ModuleResolver(ast.NodeTransformer):
    def __init__(self, modules: Dict[str, str]):
        self.modules = modules

    def visit_Attribute(self, node):
        # Traverse AST until reaching the top-level value (could be a name
        # or a function)
        cnode = node
        while isinstance(cnode.value, ast.Attribute):
            cnode = cnode.value

        if (isinstance(cnode.value, ast.Name)
                and cnode.value.id in self.modules):
            cnode.value.id = self.modules[cnode.value.id]

        return self.generic_visit(node)


def _targets(node: ast.Assign):
    for target in node.targets:
        if isinstance(target, (ast.Tuple, ast.List)):
            for elt in target.elts:
                yield elt
        else:
            yield target


# AST node types that are disallowed in DaCe programs
_DISALLOWED_STMTS = [
    'Global', 'Delete', 'Import', 'ImportFrom', 'Assert', 'Pass', 'Exec',
    'Print', 'Nonlocal', 'Yield', 'YieldFrom', 'Raise', 'Try', 'TryExcept',
    'TryFinally', 'ExceptHandler', 'Starred', 'Ellipsis', 'ClassDef',
    'AsyncFor', 'Await', 'Bytes', 'Set', 'Dict', 'ListComp', 'GeneratorExp',
    'SetComp', 'DictComp', 'comprehension'
]

MemletType = Union[ast.Call, ast.Attribute, ast.Subscript, ast.Name]
TaskletType = Union[ast.FunctionDef, ast.With, ast.For]


def _disallow_stmt(visitor, node):
    raise DaceSyntaxError(visitor, node,
                          'Keyword "%s" disallowed' % (type(node).__name__))


###############################################################
# Parsing functions
###############################################################


def _inner_eval_ast(defined, node, additional_syms=None):
    if isinstance(node, ast.AST):
        code = astutils.unparse(node)
    else:
        return node

    syms = {}
    syms.update(defined)
    if additional_syms is not None:
        syms.update(additional_syms)

    # First try to evaluate normally
    try:
        return eval(code, syms)
    except:  # Literally anything can happen here
        # If doesn't work, try to evaluate as a sympy expression
        # Replace subscript expressions with function calls (sympy support)
        code = code.replace('[', '(')
        code = code.replace(']', ')')
        return pystr_to_symbolic(code)


def _pyexpr_to_symbolic(defined_arrays_and_symbols: Dict[str, Any],
                        expr_ast: ast.AST):
    """ Converts a Python AST expression to a DaCe symbolic expression
        with error checks (raises `SyntaxError` on failure).
        @param defined_arrays_and_symbols: Defined arrays and symbols
               in the context of this expression.
        @param expr_ast: The Python AST expression to convert.
        @return: Symbolic expression.
    """
    # TODO!
    return _inner_eval_ast(defined_arrays_and_symbols, expr_ast)


def _ndslice_to_subset(ndslice):
    is_tuple = [isinstance(x, tuple) for x in ndslice]
    if not any(is_tuple):
        return subsets.Indices(ndslice)
    else:
        if not all(is_tuple):
            # If a mix of ranges and indices is found, convert to range
            for i in range(len(ndslice)):
                if not is_tuple[i]:
                    ndslice[i] = (ndslice[i], ndslice[i], 1)
        return subsets.Range(ndslice)


def _fill_missing_slices(das, ast_ndslice, array, indices):
    # Filling ndslice with default values from array dimensions
    # if ranges not specified (e.g., of the form "A[:]")
    ndslice = [None] * len(array.shape)
    ndslice_size = 1
    offsets = []
    idx = 0
    for i, dim in enumerate(ast_ndslice):
        if isinstance(dim, tuple):
            rb = _pyexpr_to_symbolic(das, dim[0] or 0)
            re = _pyexpr_to_symbolic(das, dim[1]
                                     or array.shape[indices[idx]]) - 1
            rs = _pyexpr_to_symbolic(das, dim[2] or 1)
            ndslice[i] = (rb, re, rs)
            offsets.append(i)
            idx += 1
        else:
            ndslice[i] = _pyexpr_to_symbolic(das, dim)

    # Extend slices to unspecified dimensions
    for i in range(len(ast_ndslice), len(array.shape)):
        # ndslice[i] = (0, array.shape[idx] - 1, 1)
        # idx += 1
        ndslice[i] = (0, array.shape[i] - 1, 1)
        offsets.append(i)

    return ndslice, offsets


MemletExpr = namedtuple('MemletExpr',
                        ['name', 'accesses', 'wcr', 'wcr_identity', 'subset'])


def _parse_memlet_subset(array: data.Data,
                         node: Union[ast.Name, ast.Subscript],
                         das: Dict[str, Any]):
    array_dependencies = {}

    # Get memlet range
    ndslice = [(0, s - 1, 1) for s in array.shape]
    if isinstance(node, ast.Subscript):
        # Parse and evaluate ND slice(s) (possibly nested)
        ast_ndslices = astutils.subscript_to_ast_slice_recursive(node)
        offsets = list(range(len(array.shape)))

        # Loop over nd-slices (A[i][j][k]...)
        subset_array = []
        for ast_ndslice in ast_ndslices:
            # Cut out dimensions that were indexed in the previous slice
            narray = copy.deepcopy(array)
            narray.shape = [
                s for i, s in enumerate(array.shape) if i in offsets
            ]

            # Loop over the N dimensions
            ndslice, offsets = _fill_missing_slices(das, ast_ndslice, narray,
                                                    offsets)
            subset_array.append(_ndslice_to_subset(ndslice))

        subset = subset_array[0]

        # Compose nested indices, e.g., of the form "A[i,:,j,:][k,l]"
        for i in range(1, len(subset_array)):
            subset = subset.compose(subset_array[i])

        # Compute additional array dependencies (as a result of
        # indirection)
        # for dim in subset:
        #     if not isinstance(dim, tuple): dim = [dim]
        #     for r in dim:
        #         for expr in symbolic.swalk(r):
        #             if symbolic.is_sympy_userfunction(expr):
        #                 arr = expr.func.__name__
        #                 array_dependencies[arr] = self.curnode.globals[arr]

    else:  # Use entire range
        subset = _ndslice_to_subset(ndslice)

    return subset


# Parses a memlet statement
def ParseMemlet(visitor, defined_arrays_and_symbols: Dict[str, Any],
                node: MemletType):
    das = defined_arrays_and_symbols
    arrname = rname(node)
    if arrname not in das:
        raise DaceSyntaxError(visitor, node,
                              'Use of undefined data "%s" in memlet' % arrname)
    array = das[arrname]

    # Determine number of accesses to the memlet (default is the slice size)
    num_accesses = None
    write_conflict_resolution = None
    wcr_identity = None
    # Detects expressions of the form "A(2)[...]", "A(300)", "A(1, sum)[:]"
    if isinstance(node, ast.Call):
        if len(node.args) < 1 or len(node.args) > 3:
            raise DaceSyntaxError(
                visitor, node,
                'Number of accesses in memlet must be a number, symbolic '
                'expression, or -1 (dynamic)')
        num_accesses = _pyexpr_to_symbolic(das, node.args[0])
        if len(node.args) >= 2:
            write_conflict_resolution = node.args[1]
    elif isinstance(node, ast.Subscript) and isinstance(node.value, ast.Call):
        if len(node.value.args) < 1 or len(node.value.args) > 3:
            raise DaceSyntaxError(
                visitor, node,
                'Number of accesses in memlet must be a number, symbolic '
                'expression, or -1 (dynamic)')
        num_accesses = _pyexpr_to_symbolic(das, node.value.args[0])
        if len(node.value.args) >= 2:
            write_conflict_resolution = node.value.args[1]

    subset = _parse_memlet_subset(array, node, das)

    # If undefined, default number of accesses is the slice size
    if num_accesses is None:
        num_accesses = subset.num_elements()

    return MemletExpr(arrname, num_accesses, write_conflict_resolution,
                      wcr_identity, subset)


def _parse_memlet(visitor, src: MemletType, dst: MemletType,
                  defined_arrays_and_symbols: Dict[str, data.Data]):
    srcexpr, dstexpr, localvar = None, None, None
    if isinstance(src,
                  ast.Name) and rname(src) not in defined_arrays_and_symbols:
        localvar = rname(src)
    else:
        srcexpr = ParseMemlet(visitor, defined_arrays_and_symbols, src)
    if isinstance(dst,
                  ast.Name) and rname(dst) not in defined_arrays_and_symbols:
        if localvar is not None:
            raise DaceSyntaxError(
                visitor, src,
                'Memlet source and destination cannot both be local variables')
        localvar = rname(dst)
    else:
        dstexpr = ParseMemlet(visitor, defined_arrays_and_symbols, dst)

    if srcexpr is not None and dstexpr is not None:
        # Create two memlets
        raise NotImplementedError
    elif srcexpr is not None:
        expr = srcexpr
    else:
        expr = dstexpr

    return localvar, Memlet(
        expr.name,
        expr.accesses,
        expr.subset,
        1,
        wcr=expr.wcr,
        wcr_identity=expr.wcr_identity)


def _subset_has_indirection(subset):
    for dim in subset:
        if not isinstance(dim, tuple):
            dim = [dim]
        for r in dim:
            if symbolic.contains_sympy_functions(r):
                return True
    return False


def add_indirection_subgraph(sdfg: SDFG, graph: SDFGState, src: nodes.Node,
                             dst: nodes.Node, memlet: Memlet, local_name: str,
                             PVisitor):
    """ Replaces the specified edge in the specified graph with a subgraph that
        implements indirection without nested memlet subsets. """

    array = sdfg.arrays[memlet.data]
    indirect_inputs = set()
    indirect_outputs = set()

    # Scheme for multi-array indirection:
    # 1. look for all arrays and accesses, create set of arrays+indices
    #    from which the index memlets will be constructed from
    # 2. each separate array creates a memlet, of which num_accesses = len(set)
    # 3. one indirection tasklet receives them all + original array and
    #    produces the right output index/range memlet
    #########################
    # Step 1
    accesses = OrderedDict()
    newsubset = copy.deepcopy(memlet.subset)
    for dimidx, dim in enumerate(memlet.subset):
        # Range/Index disambiguation
        direct_assignment = False
        if not isinstance(dim, tuple):
            dim = [dim]
            direct_assignment = True
        elif dim[0] == dim[1]:
            dim = [dim[0]]
            direct_assignment = True

        for i, r in enumerate(dim):
            for expr in symbolic.swalk(r, enter_functions=True):
                if symbolic.is_sympy_userfunction(expr):
                    fname = expr.func.__name__
                    if fname not in accesses:
                        accesses[fname] = []

                    # Replace function with symbol (memlet local name to-be)
                    if expr.args in accesses[fname]:
                        aindex = accesses[fname].index(expr.args)
                        toreplace = 'index_' + fname + '_' + str(aindex)
                    else:
                        accesses[fname].append(expr.args)
                        toreplace = 'index_' + fname + '_' + str(
                            len(accesses[fname]) - 1)

                    if direct_assignment:
                        # newsubset[dimidx] = newsubset[dimidx].subs(expr, toreplace)
                        newsubset[dimidx] = r.subs(expr, toreplace)
                    else:
                        rng = list(newsubset[dimidx])
                        rng[i] = rng[i].subs(expr, toreplace)
                        newsubset[dimidx] = tuple(rng)
                        # newsubset[dimidx][i] = r.subs(expr, toreplace)
    #########################
    # Step 2
    ind_inputs = {'__ind_' + local_name}
    ind_outputs = {'lookup'}
    # Add accesses to inputs
    for arrname, arr_accesses in accesses.items():
        for i in range(len(arr_accesses)):
            ind_inputs.add('index_%s_%d' % (arrname, i))

    tasklet = nodes.Tasklet("Indirection", ind_inputs, ind_outputs)

    input_index_memlets = []
    for arrname, arr_accesses in accesses.items():
        arr_name = arrname
        for i, access in enumerate(arr_accesses):
            if isinstance(access, (list, tuple)):
                access = access[0]
            if isinstance(access, sympy.Tuple):
                access = list(access)
            if not isinstance(access, (list, tuple)):
                access = [access]
            conn = None
            if PVisitor.nested:
                arr_rng = dace.subsets.Range([(a, a, 1) for a in access])
                arrname, _ = PVisitor._add_read_access(
                    arr_name, arr_rng, target=None)
                access = [0] * len(access)
                conn = 'index_%s_%d' % (arr_name, i)
            arr = sdfg.arrays[arrname]
            # Memlet to load the indirection index
            indexMemlet = Memlet(arrname, 1, subsets.Indices(access), 1)
            input_index_memlets.append(indexMemlet)
            read_node = graph.add_read(arrname)
            if PVisitor.nested:
                path = [read_node, tasklet]
            else:
                path = [read_node, src, tasklet]
            graph.add_memlet_path(
                *path,
                dst_conn="index_%s_%d" % (arr_name, i),
                memlet=indexMemlet)

    #########################
    # Step 3
    # Create new tasklet that will perform the indirection
    tasklet.code = "lookup = {arr}[{index}]".format(
        arr='__ind_' + local_name,
        index=', '.join([symbolic.symstr(s) for s in newsubset]))

    # Create transient variable to trigger the indirect load
    if memlet.num_accesses == 1:
        storage = sdfg.add_scalar(
            '__' + local_name + '_value',
            array.dtype,
            transient=True)
    else:
        storage = sdfg.add_array(
            '__' + local_name + '_value',
            memlet.bounding_box_size(),
            array.dtype,
            storage=dtypes.StorageType.Default,
            transient=True)
    indirectRange = subsets.Range([(0, s - 1, 1) for s in storage.shape])
    # dataNode = nodes.AccessNode('__' + local_name + '_value')

    # Create memlet that depends on the full array that we look up in
    fullRange = subsets.Range([(0, s - 1, 1) for s in array.shape])
    fullMemlet = Memlet(memlet.data, memlet.num_accesses, fullRange,
                        memlet.veclen)

    if isinstance(src, nodes.EntryNode):
        full_read_node = graph.add_read(memlet.data)
        graph.add_memlet_path(
            full_read_node,
            src,
            tasklet,
            dst_conn='__ind_' + local_name,
            memlet=fullMemlet)
    elif isinstance(src, nodes.AccessNode):
        graph.add_memlet_path(
            src, tasklet, dst_conn='__ind_' + local_name, memlet=fullMemlet)
    else:
        raise Exception("Src node type for indirection is invalid.")

    # Memlet to store the final value into the transient, and to load it into
    # the tasklet that needs it
    # indirectMemlet = Memlet('__' + local_name + '_value', memlet.num_accesses,
    #                         indirectRange, memlet.veclen)
    # graph.add_edge(tasklet, 'lookup', dataNode, None, indirectMemlet)

    valueMemlet = Memlet('__' + local_name + '_value', memlet.num_accesses,
                         indirectRange, memlet.veclen)
    graph.add_edge(tasklet, 'lookup', dst, local_name, valueMemlet)


class GlobalResolver(ast.NodeTransformer):
    """ Resolves global constants and lambda expressions if not
        already defined in the given scope. """

    def __init__(self, globals: Dict[str, Any]):
        self.globals = globals
        self.current_scope = set()

    def generic_visit(self, node: ast.AST):
        if hasattr(node, 'body') or hasattr(node, 'orelse'):
            oldscope = self.current_scope
            self.current_scope = set()
            self.current_scope.update(oldscope)
            result = super().generic_visit(node)
            self.current_scope = oldscope
            return result
        else:
            return super().generic_visit(node)

    def visit_Name(self, node: ast.Name):
        if isinstance(node.ctx, (ast.Store, ast.AugStore)):
            self.current_scope.add(node.id)
        else:
            if node.id in self.current_scope:
                return node
            if node.id in self.globals:
                return ast.copy_location(
                    ast.Num(n=self.globals[node.id]), node)
        return node


class TaskletTransformer(ExtNodeTransformer):
    """ A visitor that traverses a data-centric tasklet, removes memlet 
        annotations and returns input and output memlets. 
    """

    def __init__(self,
                 defined,
                 sdfg: SDFG,
                 state: SDFGState,
                 filename: str,
                 lang=dtypes.Language.Python,
                 location: str = '-1',
                 nested: bool = False,
                 scope_arrays: Dict[str, data.Data] = dict(),
                 scope_vars: Dict[str, str] = dict(),
                 variables: Dict[str, str] = dict(),
                 accesses: Dict[Tuple[str, dace.subsets.Subset, str],
                                str] = dict()):
        """ Creates an AST parser for tasklets. 
            @param sdfg: The SDFG to add the tasklet in (used for defined arrays and symbols).
            @param state: The SDFG state to add the tasklet to.
        """
        self.sdfg = sdfg
        self.state = state
        self.defined = defined

        # For syntax errors
        self.filename = filename

        # Connectors generated from memlets
        self.inputs = {}  # type: Dict[str, Memlet]
        self.outputs = {}  # type: Dict[str, Memlet]

        self.extcode = None
        self.lang = lang
        self.globalcode = ''
        self.initcode = ''
        self.exitcode = ''
        self.location = location

        self.nested = nested
        self.scope_arrays = scope_arrays
        self.scope_vars = scope_vars
        self.variables = variables
        self.accesses = accesses

        self.sdfg_inputs = {}
        self.sdfg_outputs = {}

        # Disallow keywords
        for stmt in _DISALLOWED_STMTS:
            setattr(self, 'visit_' + stmt, lambda n: _disallow_stmt(self, n))

    def parse_tasklet(self, tasklet_ast: TaskletType):
        """ Parses the AST of a tasklet and returns the tasklet node, as well as input and output memlets. 
            @param tasklet_ast: The Tasklet's Python AST to parse.
            @return: 3-tuple of (Tasklet node, input memlets, output memlets).
            @rtype: Tuple[Tasklet, Dict[str, Memlet], Dict[str, Memlet]]
        """
        # Should return a tasklet object (with connectors)
        self.visit(tasklet_ast)

        # Location identifier
        locinfo = dtypes.DebugInfo(tasklet_ast.lineno, tasklet_ast.col_offset,
                                   tasklet_ast.body[-1].lineno,
                                   tasklet_ast.body[-1].col_offset,
                                   self.filename)

        # Determine tasklet name (either declared as a function or use line #)
        name = getattr(tasklet_ast, 'name', 'tasklet_%d' % tasklet_ast.lineno)

        t = self.state.add_tasklet(
            name,
            set(self.inputs.keys()),
            set(self.outputs.keys()),
            self.extcode or tasklet_ast,
            language=self.lang,
            code_global=self.globalcode,
            code_init=self.initcode,
            code_exit=self.exitcode,
            location=self.location,
            debuginfo=locinfo)

        return t, self.inputs, self.outputs

    def visit_TopLevelExpr(self, node):
        if isinstance(node.value, ast.BinOp):
            if isinstance(node.value.op, (ast.LShift, ast.RShift)):
                if isinstance(node.value.op, ast.LShift):
                    target = node.value.right
                    has_indirection = False
                    if self.nested:  # and isinstance(target, ast.Subscript):
                        name = rname(target)
                        real_name = {**self.variables, **self.scope_vars}[name]
                        if isinstance(target, ast.Subscript):
                            real_target = copy.deepcopy(target)
                            real_target.value.id = real_name
                            rng = dace.subsets.Range(
                                astutils.subscript_to_slice(
                                    real_target, {
                                        **self.sdfg.arrays,
                                        **self.scope_arrays
                                    })[1])
                        elif isinstance(target, ast.Call):
                            rng = dace.subsets.Range.from_array({
                                **self.sdfg.arrays,
                                **self.scope_arrays
                            }[real_name])
                        else:
                            raise NotImplementedError
                        if (name, rng, 'w') in self.accesses:
                            node.value.right = ast.Name(id=self.accesses[(
                                name, rng, 'w')][0])
                        elif (name, rng, 'r') in self.accesses:
                            node.value.right = ast.Name(id=self.accesses[(
                                name, rng, 'r')][0])
                        elif name in self.variables:
                            pass
                        elif name in self.scope_vars:
                            vname = "__tmp_{l}_{c}".format(
                                l=target.lineno, c=target.col_offset)
                            parent_name = self.scope_vars[name]
                            parent_array = self.scope_arrays[parent_name]
                            sqz_rng = copy.deepcopy(rng)
                            if _subset_has_indirection(rng):
                                has_indirection = True
                                non_sqz = list(range(len(rng)))
                                shape = parent_array.shape
                            else:
                                non_sqz = sqz_rng.squeeze()
                                shape = sqz_rng.size()
                            dtype = parent_array.dtype
                            strides = [
                                parent_array.strides[d] for d in non_sqz
                            ]
                            self.sdfg.add_array(
                                vname, shape, dtype, strides=strides)
                            self.accesses[(name, rng, 'r')] = (vname, sqz_rng)
                            if _subset_has_indirection(rng):
                                self.sdfg_inputs[vname] = (
                                    dace.Memlet.from_array(
                                        parent_name, parent_array), set())
                            else:
                                self.sdfg_inputs[vname] = (dace.Memlet(
                                    parent_name, rng.num_elements(), rng, 1),
                                                           set())
                            # self.defined[vname] = self.sdfg.arrays[vname]
                            if isinstance(target, ast.Subscript):
                                node.value.right = ast.Name(id=vname)
                            elif isinstance(target, ast.Call):
                                node.value.right.func = ast.Name(id=vname)
                            else:
                                raise NotImplementedError
                        else:
                            raise DaceSyntaxError(
                                self, target,
                                'Array "{}" used before definition'.format(
                                    name))
                    connector, memlet = _parse_memlet(self, node.value.right,
                                                      node.value.left,
                                                      self.sdfg.arrays)
                    if has_indirection:
                        memlet = dace.Memlet(memlet.data, rng.num_elements(),
                                             rng, 1)
                    if connector in self.inputs or connector in self.outputs:
                        raise DaceSyntaxError(
                            self, node,
                            'Local variable is already a tasklet input or output'
                        )
                    self.inputs[connector] = memlet
                elif isinstance(node.value.op, ast.RShift):
                    target = node.value.right
                    new_output = False
                    if self.nested:  # and isinstance(target, ast.Subscript):
                        name = rname(target)
                        real_name = {**self.variables, **self.scope_vars}[name]
                        if isinstance(target, ast.Subscript):
                            real_target = copy.deepcopy(target)
                            real_target.value.id = real_name
                            rng = dace.subsets.Range(
                                astutils.subscript_to_slice(
                                    real_target, {
                                        **self.sdfg.arrays,
                                        **self.scope_arrays
                                    })[1])
                        elif isinstance(target, ast.Call):
                            rng = dace.subsets.Range.from_array({
                                **self.sdfg.arrays,
                                **self.scope_arrays
                            }[real_name])
                        else:
                            raise NotImplementedError
                        if (name, rng, 'w') in self.accesses:
                            node.value.right = ast.Name(id=self.accesses[(
                                name, rng, 'w')][0])
                        elif name in self.variables:
                            pass
                        elif ((name, rng, 'r') in self.accesses
                              or name in self.scope_vars):
                            vname = "__tmp_{l}_{c}".format(
                                l=target.lineno, c=target.col_offset)
                            parent_name = self.scope_vars[name]
                            parent_array = self.scope_arrays[parent_name]
                            sqz_rng = copy.deepcopy(rng)
                            non_sqz = sqz_rng.squeeze()
                            shape = sqz_rng.size()
                            dtype = parent_array.dtype
                            strides = [
                                parent_array.strides[d] for d in non_sqz
                            ]
                            self.sdfg.add_array(
                                vname, shape, dtype, strides=strides)
                            self.accesses[(name, rng, 'w')] = (vname, sqz_rng)
                            self.sdfg_outputs[vname] = (dace.Memlet(
                                parent_name, rng.num_elements(), rng, 1),
                                                        set())
                            # self.defined[vname] = self.sdfg.arrays[vname]
                            new_output = True
                            if isinstance(target, ast.Subscript):
                                if isinstance(target.value, ast.Call):
                                    node.value.right = node.value.right.value
                                    node.value.right.func = ast.Name(id=vname)
                                else:
                                    node.value.right = ast.Name(id=vname)
                            elif isinstance(target, ast.Call):
                                node.value.right.func = ast.Name(id=vname)
                            else:
                                raise NotImplementedError
                        else:
                            raise DaceSyntaxError(
                                self, target,
                                'Array "{}" used before definition'.format(
                                    name))
                    connector, memlet = _parse_memlet(self, node.value.left,
                                                      node.value.right,
                                                      self.sdfg.arrays)
                    if new_output:
                        out_memlet = self.sdfg_outputs[vname][0]
                        out_memlet.wcr = memlet.wcr
                        out_memlet.wcr_identity = memlet.wcr_identity
                        out_memlet.wcr_conflict = memlet.wcr_conflict
                    if connector in self.inputs or connector in self.outputs:
                        raise DaceSyntaxError(
                            self, node,
                            'Local variable is already a tasklet input or output'
                        )
                    self.outputs[connector] = memlet
        elif isinstance(node.value, ast.Str):
            return self.visit_TopLevelStr(node.value)

        return self.generic_visit(node)

    # Detect external tasklet code
    def visit_TopLevelStr(self, node: ast.Str):
        if self.extcode != None:
            raise DaceSyntaxError(
                self, node,
                'Cannot provide more than one intrinsic implementation ' +
                'for tasklet')
        self.extcode = node.s
        # TODO(later): Syntax for other languages?
        self.lang = dtypes.Language.CPP

        return node


# TODO: Take care of recursive SDFG generation w.r.t. temporary transient creation (maybe there
#  is no need if the temporary transients from the parent SDFG are added to the current SDFG arrays)


class ProgramVisitor(ExtNodeVisitor):
    """ A visitor that traverses a data-centric Python program AST and 
        constructs an SDFG.
    """

    def __init__(self,
                 name: str,
                 filename: str,
                 lineoffset: int,
                 arrays: Dict[str, data.Data],
                 global_vars: Dict[str, Any],
                 variables: Dict[str, str],
                 constants: Dict[str, Any],
                 other_sdfgs: Dict[str, Any],
                 nested: bool = False):  # Actually Union[SDFG, DaceProgram]
        self.curnode = None
        self.filename = filename
        self.lineoffset = lineoffset
        self.globals = global_vars
        self.nested = nested
        self.other_sdfgs = other_sdfgs

        self.scope_arrays = OrderedDict()  # type: Dict[str, data.Data]
        self.scope_arrays.update(arrays)

        # Entry point to the program
        self.program = None
        self.sdfg = SDFG(name)
        self.last_state = self.sdfg.add_state('init', is_start_state=True)
        if not self.nested:
            self.sdfg.arrays.update(arrays)
        self.inputs = {}
        self.outputs = {}

        # Define symbols
        for k, v in arrays.items():
            if isinstance(v, data.Scalar):
                self.sdfg.add_symbol(k, v.dtype)

        # Define constants
        self.sdfg.add_constants(constants)
        # Define numbers
        self.numbers = dict()

        # Keep track of variables and scopes
        self.scope_vars = {k: k for k in arrays.keys()}  # type: Dict[str, str]
        self.scope_vars.update(variables)
        self.variables = dict()
        self.accesses = dict()

        # Add symbols. TODO: more elegant way
        for arr in arrays.values():
            if arr is None:
                continue
            for dim in arr.shape:
                if not hasattr(dim, 'free_symbols'): continue
                self.scope_vars.update(
                    {str(k): self.globals[str(k)]
                     for k in dim.free_symbols})

        # Disallow keywords
        for stmt in _DISALLOWED_STMTS:
            setattr(self, 'visit_' + stmt, lambda n: _disallow_stmt(self, n))

    def parse_program(self, program: ast.FunctionDef, is_tasklet=False):
        if is_tasklet:
            program.decorator_list = []
            self.visit_FunctionDef(program)
        else:
            for stmt in program.body:
                self.visit_TopLevel(stmt)
        if len(self.sdfg.nodes()) == 0:
            self.sdfg.add_state("EmptyState")

        def fix_memlet(memlet: dace.Memlet, vars: Dict[str, str]):
            fixed_memlet = copy.deepcopy(memlet)
            try:
                fixed_memlet.data = vars[memlet.data]
            except:
                pass
            return fixed_memlet

        # if not self.nested:
        # TODO: Incremental, union-of-subset creation of self.inputs and self.outputs
        # inputs = {}
        # outputs = {}
        # for state in self.sdfg.nodes():
        #     inputs.update({
        #         n.data: fix_memlet(state.out_edges(n)[0].data, self.variables)
        #         for n in state.source_nodes()
        #         if isinstance(n, nodes.AccessNode) and (
        #             self.sdfg.arrays[n.data].transient == False
        #             or n.data in self.scope_arrays)
        #     })
        # for state in self.sdfg.nodes():
        #     outputs.update({
        #         n.data: fix_memlet(state.in_edges(n)[0].data, self.variables)
        #         for n in state.sink_nodes()
        #         if isinstance(n, nodes.AccessNode) and (
        #             self.sdfg.arrays[n.data].transient == False
        #             or n.data in self.scope_arrays)
        #     })
        # else:
        #     inputs = {}
        #     for conn, (arr_name, subset, inner_indices) in self.inputs.items():
        #         inputs[conn] = (dace.Memlet(arr_name, subset.num_elements(),
        #                                     subset, 1), inner_indices)
        #     outputs = {}
        #     # for conn, (arr_name, subset, inner_indices) in self.outputs.items():
        #     for conn, arr_name in self.outputs.items():
        #         # outputs[conn] = (dace.Memlet(arr_name, subset.num_elements(),
        #         #                              subset, 1), inner_indices)
        #         outputs[conn] = dace.Memlet.from_array(arr_name, self.scope_arrays[arr_name])

        return self.sdfg, self.inputs, self.outputs

    @property
    def defined(self):
        # Check parent SDFG arrays first
        # result = {
        #     k: self.parent_arrays[v]
        #     for k, v in self.variables.items() if v in self.parent_arrays
        # }
        result = {}
        result.update({
            k: self.sdfg.arrays[v]
            for k, v in self.scope_vars.items() if v in self.sdfg.arrays
        })
        result.update({
            k: self.sdfg.arrays[v]
            for k, v in self.variables.items() if v in self.sdfg.arrays
        })
        # TODO: Is there a case of a variable-symbol?
        result.update({
            k: self.sdfg.symbols[v]
            for k, v in self.variables.items() if v in self.sdfg.symbols
        })

        return result

    def _add_state(self, label=None):
        state = self.sdfg.add_state(label)
        if self.last_state is not None:
            self.sdfg.add_edge(self.last_state, state, dace.InterstateEdge())
        self.last_state = state
        return state

    def _parse_arg(self, arg: Any, as_list=True):
        """ Parse possible values to slices or objects that can be used in
            the SDFG API. """
        if isinstance(arg, ast.Subscript) and rname(arg) == '_':
            # result = [
            #     ':'.join([str(d) for d in dim]) for dim in
            #     astutils.subscript_to_slice(arg, self.sdfg.arrays)[1]
            # ]
            rng = dace.subsets.Range(
                astutils.subscript_to_slice(arg, self.sdfg.arrays)[1])
            result = rng.string_list()
            if as_list is False and len(result) == 1:
                return result[0]
            return result

        return arg

    def _decorator_or_annotation_params(
            self, node: ast.FunctionDef) -> List[Tuple[str, Any]]:
        """ Returns a list of parameters, either from the function parameters
            and decorator arguments or parameters and their annotations (type
            hints).
            @param node: The given function definition node.
            @return: A list of 2-tuples (name, value).
        """
        # If the arguments are defined in the decorator
        dec = node.decorator_list[0]
        if 'args' in dir(dec) and len(dec.args) > 0:
            # If it's one argument of the form of ND range, e.g., "_[0:M, 0:N]"
            parg0 = self._parse_arg(dec.args[0])
            if isinstance(parg0, list):
                args = parg0
            else:
                args = [self._parse_arg(arg) for arg in dec.args]
        else:  # Otherwise, use annotations
            args = [
                self._parse_arg(arg.annotation, as_list=False)
                for arg in node.args.args
            ]

        result = [(rname(arg), argval)
                  for arg, argval in zip(node.args.args, args)]

        # Ensure all arguments are annotated
        if len(result) != len(node.args.args):
            raise DaceSyntaxError(
                self, node,
                'All arguments in primitive %s must be annotated' % node.name)
        return result

    def _parse_subprogram(self, name, node, is_tasklet=False):
        scope_arrays = {**self.scope_arrays, **self.sdfg.arrays}
        scope_vars = {**self.scope_vars, **self.variables}
        pv = ProgramVisitor(
            name,
            self.filename,
            self.lineoffset,
            scope_arrays,
            self.globals,
            scope_vars, {},
            self.other_sdfgs,
            nested=True)

        return pv.parse_program(node, is_tasklet)

    def visit_FunctionDef(self, node: ast.FunctionDef):
        # Supported decorated function types: map, mapscope, consume,
        # consumescope, tasklet, program

        if len(node.decorator_list) > 1:
            raise DaceSyntaxError(
                self, node,
                'Exactly one DaCe decorator is allowed on a function')
        if len(node.decorator_list) == 0:
            dec = 'dace.tasklet'
        else:
            dec = rname(node.decorator_list[0])

        # Create a new state for the statement
        state = self._add_state('s' + str(self.lineoffset))

        # Define internal node for reconnection
        internal_node = None

        # Select primitive according to function type
        if dec == 'dace.tasklet':  # Tasklet
            internal_node, inputs, outputs, sdfg_inp, sdfg_out = self._parse_tasklet(
                state, node)

            # Add memlets
            self._add_dependencies(state, internal_node, None, None, inputs,
                                   outputs)
            self.inputs.update(sdfg_inp)
            self.outputs.update(sdfg_out)

        elif dec.startswith('dace.map') or dec.startswith(
                'dace.consume'):  # Scope or scope+tasklet
            params = self._decorator_or_annotation_params(node)
            params, map_inputs = self._parse_map_inputs(
                node.name, params, node)
            dummy = False
            if 'map' in dec:
                entry, exit = state.add_map(node.name, ndrange=params)
            elif 'consume' in dec:
                entry, exit = state.add_consume(node.name, **params)

            if dec.endswith('scope'):  # @dace.mapscope or @dace.consumescope
                sdfg, inputs, outputs = self._parse_subprogram(node.name, node)
                internal_node = state.add_nested_sdfg(sdfg, self.sdfg,
                                                      set(inputs.keys()),
                                                      set(outputs.keys()))
            else:  # Scope + tasklet (e.g., @dace.map)
                # internal_node, inputs, outputs, sdfg_inp, sdfg_out = self._parse_tasklet(
                #     state, node)
                # dummy = True
                name = "{}_body".format(entry.label)
                # name = "{}_{}_{}_body".format(self.sdfg.label, state.label,
                #                               state.node_id(entry))
                body, inputs, outputs = self._parse_subprogram(
                    name, node, True)
                internal_node = state.add_nested_sdfg(body, self.sdfg,
                                                      inputs.keys(),
                                                      outputs.keys())

            # Connect internal node with scope/access nodes
            self._add_dependencies(state, internal_node, entry, exit, inputs,
                                   outputs, map_inputs)
            if dummy:
                self.inputs.update(sdfg_inp)
                self.outputs.update(sdfg_out)

        elif dec == 'dace.program':  # Nested SDFG
            raise DaceSyntaxError(
                self, node, 'Nested programs must be '
                'defined outside existing programs')
        else:
            raise DaceSyntaxError(self, node, 'Unsupported function decorator')

    def _parse_for_indices(self, node: ast.Expr):
        """Parses the indices of a for-loop statement
        
        Arguments:
            node {ast.Expr} -- Target of ast.For node
        
        Raises:
            DaceSyntaxError: If target is not ast.Tuple
            DaceSyntaxError: If index is not ast.Name
            DaceSyntaxError: If index ID is duplicate
        
        Returns:
            List[str] -- List of index IDs
        """

        if not isinstance(node, (ast.Name, ast.Tuple)):
            raise DaceSyntaxError(
                self, node, "Target of ast.For must be a name or a tuple")

        if isinstance(node, ast.Name):
            elts = (node, )
        else:
            elts = node.elts

        indices = []
        for n in elts:
            if not isinstance(n, ast.Name):
                raise DaceSyntaxError(self, n,
                                      "For-loop iterator must be ast.Name")
            idx_id = n.id
            if idx_id in indices:
                raise DaceSyntaxError(
                    self, n,
                    "Duplicate index id ({}) in for-loop".format(idx_id))
            indices.append(idx_id)

        return indices

    def _parse_value(self, node: Union[ast.Name, ast.Num]):
        """Parses a value
        
        Arguments:
            node {Union[ast.Name, ast.Num]} -- Value node
        
        Raises:
            DaceSyntaxError: If node is not ast.Name or ast.Num
        
        Returns:
            str -- Value id or number as string
        """

        if isinstance(node, ast.Name):
            return node.id
        elif isinstance(node, ast.Num):
            return str(node.n)
        else:
            return _pyexpr_to_symbolic(self.defined, node)
            # raise DaceSyntaxError(self, node,
            #                       "Expected ast.Name or ast.Num as value")

    def _parse_slice(self, node: ast.Slice):
        """Parses a range
        
        Arguments:
            node {ast.Slice} -- Slice node
        
        Returns:
            Tuple[str] -- Range in (from, to, step) format
        """

        return (self._parse_value(node.lower), self._parse_value(node.upper),
                self._parse_value(node.step) if node.step is not None else "1")

    def _parse_index_as_range(self, node: ast.Index):
        """Parses an index as range
        
        Arguments:
            node {ast.Index} -- Index node
        
        Returns:
            Tuple[str] -- Range in (from, to, step) format
        """

        val = self._parse_value(node.value)
        return (val, val, "1")

    def _parse_for_iterator(self, node: ast.Expr):
        """Parses the iterator of a for-loop statement
        
        Arguments:
            node {ast.Expr} -- Iterator (iter) of ast.For node
        
        Raises:
            DaceSyntaxError: If iterator is not ast.Subscript
            DaceSyntaxError: If iterator type is not supported
            NotImplementedError: If iterator type is not implemented
        
        Returns:
            Tuple[str, List[str]] -- Iterator type and iteration ranges
        """

        if not isinstance(node, (ast.Call, ast.Subscript)):
            raise DaceSyntaxError(
                self, node,
                "Iterator of ast.For must be a function or a subscript")

        iterator = rname(node)
        if iterator not in {'range', 'parrange', 'dace.map'}:
            raise DaceSyntaxError(
                self, node, "Iterator {} is unsupported".format(iterator))
        elif iterator in ['range', 'parrange']:
            if len(node.args) == 1:  # (par)range(stop)
                ranges = [('0', self._parse_value(node.args[0]), '1')]
            elif len(node.args) == 2:  # (par)range(start, stop)
                ranges = [(self._parse_value(node.args[0]),
                           self._parse_value(node.args[1]), '1')]
            elif len(node.args) == 3:  # (par)range(start, stop, step)
                ranges = [(self._parse_value(node.args[0]),
                           self._parse_value(node.args[1]),
                           self._parse_value(node.args[2]))]
            else:
                raise DaceSyntaxError(
                    self, node,
                    'Invalid number of arguments for "%s"' % iterator)
            if iterator == 'parrange':
                iterator = 'dace.map'
        else:
            ranges = []
            if isinstance(node.slice, ast.ExtSlice):
                for s in node.slice.dims:
                    ranges.append(self._parse_slice(s))
            elif isinstance(node.slice, ast.Slice):
                ranges.append(self._parse_slice(node.slice))
            else:  # isinstance(node.slice, ast.Index) is True
                ranges.append(self._parse_index_as_range(node.slice))

        return (iterator, ranges)

    def _parse_map_inputs(
            self, name: str, params: List[Tuple[str, str]],
            node: ast.AST) -> Tuple[Dict[str, str], Dict[str, Memlet]]:
        """ Parse map parameters for data-dependent inputs, modifying the
            parameter dictionary and returning relevant memlets.
            @return: A 2-tuple of (parameter dictionary, mapping from connector
                     name to memlet).
        """
        new_params = []
        map_inputs = {}
        for k, v in params:
            vsp = list(v.split(':'))
            for i, (val, vid) in enumerate(zip(vsp, 'best')):
                # Walk through expression, find functions and replace with variables
                ctr = 0
                repldict = {}
                for expr in symbolic.swalk(pystr_to_symbolic(val)):
                    if symbolic.is_sympy_userfunction(expr):
                        # If function contains a function
                        if any(
                                symbolic.contains_sympy_functions(a)
                                for a in expr.args):
                            raise DaceSyntaxError(
                                self, node,
                                'Indirect accesses not supported in map ranges'
                            )
                        arr = expr.func.__name__
                        newvar = '__%s_%s%d' % (name, vid, ctr)
                        repldict[arr] = newvar
                        # Create memlet
                        map_inputs[newvar] = Memlet.simple(
                            self.variables[arr],
                            ','.join([str(a) for a in expr.args]))
                        ctr += 1
                # Replace functions with new variables
                for find, replace in repldict.items():
                    val = re.sub(r"%s\(.*?\)" % find, replace, val)
                vsp[i] = val

            new_params.append((k, ':'.join(vsp)))

        return new_params, map_inputs

    def _add_dependencies(self,
                          state: SDFGState,
                          internal_node: nodes.CodeNode,
                          entry_node: nodes.EntryNode,
                          exit_node: nodes.ExitNode,
                          inputs: Dict[str, Memlet],
                          outputs: Dict[str, Memlet],
                          map_inputs: Dict[str, Memlet] = None):

        # Parse map inputs (for memory-based ranges)
        if map_inputs is not None:
            for conn, memlet in map_inputs.items():
                read_node = state.add_read(memlet.data)
                if _subset_has_indirection(memlet.subset):
                    add_indirection_subgraph(self.sdfg, state, read_node,
                                             entry_node, memlet, conn, self)
                    continue

                entry_node.add_in_connector(conn)
                state.add_edge(read_node, None, entry_node, conn, memlet)

        # Parse internal node inputs and indirect memory accesses
        if inputs:
            for conn, v in inputs.items():
                if isinstance(v, tuple):
                    memlet, inner_indices = v
                else:
                    memlet, inner_indices = v, set()
                if _subset_has_indirection(memlet.subset):
                    read_node = entry_node
                    if entry_node is None:
                        read_node = state.add_read(memlet.data)
                    add_indirection_subgraph(self.sdfg, state, read_node,
                                             internal_node, memlet, conn, self)
                    continue
                if memlet.data not in self.sdfg.arrays:
                    arr = self.scope_arrays[memlet.data]
                    scope_memlet = propagate_memlet(state, memlet, entry_node,
                                                    True, arr)
                    name = memlet.data
                    irng = memlet.subset
                    orng = copy.deepcopy(scope_memlet.subset)
                    outer_indices = []
                    for n, (i, o) in enumerate(zip(irng, orng)):
                        if i == o and n not in inner_indices:
                            outer_indices.append(n)
                        elif n not in inner_indices:
                            inner_indices.add(n)
                    # for n in reversed(outer_indices):
                    #     irng.ranges.pop(n)
                    #     orng.ranges.pop(n)
                    irng.pop(outer_indices)
                    orng.pop(outer_indices)
                    irng.offset(orng, True)
                    vname = "{c}_in_from_{s}_{n}".format(
                        c=conn,
                        s=self.sdfg.nodes().index(state),
                        n=state.node_id(entry_node))
                    self.accesses[(name, scope_memlet.subset, 'r')] = vname
                    orig_shape = orng.size()
                    shape = [d for d in orig_shape if d != 1]
                    strides = [i for j, i in enumerate(arr.strides)
                               if j not in outer_indices]
                    strides = [
                        s for d, s in zip(orig_shape, strides) if d != 1
                    ]
                    if not shape:
                        shape = [1]
                        strides = [1]
                    dtype = arr.dtype
                    self.sdfg.add_array(vname, shape, dtype, strides=strides)
                    self.inputs[vname] = (scope_memlet, inner_indices)
                    # self.inputs[vname] = (memlet.data, scope_memlet.subset, inner_indices)
                    memlet.data = vname
                    # memlet.subset.offset(memlet.subset, True, outer_indices)
                else:
                    vname = memlet.data
            # for conn, memlet in inputs.items():
            #     if _subset_has_indirection(memlet.subset):
            #         read_node = entry_node
            #         if entry_node is None:
            #             read_node = state.add_read(memlet.data)
            #         add_indirection_subgraph(self.sdfg, state, read_node,
            #                                  internal_node, memlet, conn)
            #         continue

                read_node = state.add_read(vname)

                if entry_node is not None:
                    state.add_memlet_path(
                        read_node,
                        entry_node,
                        internal_node,
                        memlet=memlet,
                        src_conn=None,
                        dst_conn=conn)
                else:
                    state.add_edge(read_node, None, internal_node, conn,
                                   memlet)
        else:
            if entry_node is not None:
                state.add_nedge(entry_node, internal_node, dace.EmptyMemlet())

        # Parse internal node outputs
        if outputs:
            for conn, v in outputs.items():
                if isinstance(v, tuple):
                    memlet, inner_indices = v
                else:
                    memlet, inner_indices = v, set()
                if memlet.data not in self.sdfg.arrays:
                    arr = self.scope_arrays[memlet.data]
                    scope_memlet = propagate_memlet(state, memlet, exit_node,
                                                    True, arr)
                    name = memlet.data
                    irng = memlet.subset
                    orng = copy.deepcopy(scope_memlet.subset)
                    outer_indices = []
                    for n, (i, o) in enumerate(zip(irng, orng)):
                        if i == o and n not in inner_indices:
                            outer_indices.append(n)
                        elif n not in inner_indices:
                            inner_indices.add(n)
                    # for n in reversed(outer_indices):
                    #     irng.ranges.pop(n)
                    #     orng.ranges.pop(n)
                    irng.pop(outer_indices)
                    orng.pop(outer_indices)
                    irng.offset(orng, True)
                    vname = "{c}_out_of_{s}_{n}".format(
                        c=conn,
                        s=self.sdfg.nodes().index(state),
                        n=state.node_id(exit_node))
                    self.accesses[(name, scope_memlet.subset, 'w')] = vname
                    orig_shape = orng.size()
                    shape = [d for d in orig_shape if d != 1]
                    strides = [i for j, i in enumerate(arr.strides)
                               if j not in outer_indices]
                    strides = [
                        s for d, s in zip(orig_shape, strides) if d != 1
                    ]
                    if not shape:
                        shape = [1]
                        strides = [1]
                    dtype = arr.dtype
                    self.sdfg.add_array(vname, shape, dtype, strides=strides)
                    self.outputs[vname] = (scope_memlet, inner_indices)
                    # self.outputs[vname] = (memlet.data, scope_memlet.subset, inner_indices)
                    memlet.data = vname
                    # memlet.subset.offset(memlet.subset, True, outer_indices)
                else:
                    vname = memlet.data
                write_node = state.add_write(vname)
                if exit_node is not None:
                    state.add_memlet_path(
                        internal_node,
                        exit_node,
                        write_node,
                        memlet=memlet,
                        src_conn=conn,
                        dst_conn=None)
                else:
                    state.add_edge(internal_node, conn, write_node, None,
                                   memlet)
        else:
            if exit_node is not None:
                state.add_nedge(internal_node, exit_node, dace.EmptyMemlet())

    def _recursive_visit(self,
                         body: List[ast.AST],
                         name: str,
                         lineno: int,
                         last_state=True):
        """ Visits a subtree of the AST, creating special states before and after the visit.
            Returns the previous state, and the first and last internal states of the
            recursive visit. """
        before_state = self.last_state
        self.last_state = None
        first_internal_state = self._add_state('%s_%d' % (name, lineno))

        # Recursive loop processing
        for stmt in body:
            self.visit_TopLevel(stmt)

        # Create the next state
        last_internal_state = self.last_state
        if last_state:
            self.last_state = None
            self._add_state('end%s_%d' % (name, lineno))

        return before_state, first_internal_state, last_internal_state

    def visit_For(self, node: ast.For):
        # We allow three types of for loops:
        # 1. `for i in range(...)`: Creates a looping state
        # 2. `for i in parrange(...)`: Creates a 1D map
        # 3. `for i,j,k in dace.map[0:M, 0:N, 0:K]`: Creates an ND map
        # print(ast.dump(node))
        indices = self._parse_for_indices(node.target)
        iterator, ranges = self._parse_for_iterator(node.iter)

        if len(indices) != len(ranges):
            raise DaceSyntaxError(
                self, node,
                "Number of indices and ranges of for-loop do not match")

        if iterator == 'dace.map':
            state = self._add_state('MapState')
            params = [(k, ':'.join(v)) for k, v in zip(indices, ranges)]
            params, map_inputs = self._parse_map_inputs(
                'map_%d' % node.lineno, params, node)
            me, mx = state.add_map(name='Map', ndrange=params)
            # body = SDFG('MapBody')
            body, inputs, outputs = self._parse_subprogram('MapBody', node)
            tasklet = state.add_nested_sdfg(body, self.sdfg, inputs.keys(),
                                            outputs.keys())
            self._add_dependencies(state, tasklet, me, mx, inputs, outputs,
                                   map_inputs)
        elif iterator == 'range':
            # Add an initial loop state with a None last_state (so as to not
            # create an interstate edge)
            laststate, first_loop_state, last_loop_state = \
                self._recursive_visit(node.body, 'for', node.lineno)
            end_loop_state = self.last_state

            # Add loop to SDFG
            loop_cond = '>' if ((pystr_to_symbolic(ranges[0][2]) <
                                 0) == True) else '<'
            self.sdfg.add_loop(
                laststate, first_loop_state, end_loop_state, indices[0],
                ranges[0][0],
                '%s %s %s' % (indices[0], loop_cond, ranges[0][1]),
                '%s + %s' % (indices[0], ranges[0][2]), last_loop_state)

    def visit_While(self, node: ast.While):
        # Add an initial loop state with a None last_state (so as to not
        # create an interstate edge)
        laststate, first_loop_state, last_loop_state = \
            self._recursive_visit(node.body, 'while', node.lineno)
        end_loop_state = self.last_state

        # Add loop to SDFG
        loop_cond = astutils.unparse(node.test)
        self.sdfg.add_loop(laststate, first_loop_state, end_loop_state, None,
                           None, loop_cond, None, last_loop_state)

    def visit_If(self, node: ast.If):
        # Add a guard state
        self._add_state('if_guard')

        # Visit recursively
        laststate, first_if_state, last_if_state = \
            self._recursive_visit(node.body, 'if', node.lineno)
        end_if_state = self.last_state

        # Connect the states
        cond = astutils.unparse(node.test)
        cond_else = astutils.unparse(astutils.negate_expr(node.test))
        self.sdfg.add_edge(laststate, first_if_state,
                           dace.InterstateEdge(cond))
        self.sdfg.add_edge(last_if_state, end_if_state, dace.InterstateEdge())

        # Process 'else'/'elif' statements
        if len(node.orelse) > 0:
            # Visit recursively
            _, first_else_state, last_else_state = \
                self._recursive_visit(node.orelse, 'else', node.lineno, False)

            # Connect the states
            self.sdfg.add_edge(laststate, first_else_state,
                               dace.InterstateEdge(cond_else))
            self.sdfg.add_edge(last_else_state, end_if_state,
                               dace.InterstateEdge())
        else:
            self.sdfg.add_edge(laststate, end_if_state,
                               dace.InterstateEdge(cond_else))

    def _parse_index(self, node: ast.Index):

        indices = []
        for idx in node.value.elts:
            indices.append(self._parse_value(idx))

        return indices

    def _parse_tasklet(self, state: SDFGState, node: TaskletType):
        ttrans = TaskletTransformer(
            self.defined,
            self.sdfg,
            state,
            self.filename,
            nested=self.nested,
            scope_arrays=self.scope_arrays,
            scope_vars=self.scope_vars,
            variables=self.variables,
            accesses=self.accesses)
        node, inputs, outputs = ttrans.parse_tasklet(node)

        # Convert memlets to their actual data nodes
        for i in inputs.values():
            if not isinstance(i, tuple) and i.data in self.scope_vars.keys():
                i.data = self.scope_vars[i.data]
        for o in outputs.values():
            if not isinstance(o, tuple) and o.data in self.scope_vars.keys():
                o.data = self.scope_vars[o.data]
        return node, inputs, outputs, ttrans.sdfg_inputs, ttrans.sdfg_outputs

    def _add_tasklet(self, target, operand):
        # value = self._parse_value(value_node)
        print(target, operand)

        if isinstance(target, tuple):
            name, subset = target
            memlet = dace.Memlet(name, subset.num_elements(), subset, 1)
        else:
            name = target
            memlet = dace.Memlet.from_array(name, self.sdfg.arrays[name])
        if isinstance(operand, tuple):
            rname, rsubset = operand
        else:
            rname, rsubset = operand, None

        state = self._add_state("TaskletState")
        if rname in self.sdfg.arrays:
            inputs = {"inp"}
            code = "out = inp"
        else:
            inputs = {}
            code = "out = {}".format(rname)
        write_node = state.add_write(name)
        tasklet_node = state.add_tasklet(
            name="Tasklet", inputs=inputs, outputs={"out"}, code=code)
        if rname in self.sdfg.arrays:
            read_node = state.add_read(rname)
            if rsubset:
                rmemlet = dace.Memlet(rname, rsubset.num_elements(), rsubset,
                                      1)
            else:
                rmemlet = dace.Memlet.from_array(rname,
                                                 self.sdfg.arrays[rname])
            state.add_edge(read_node, None, tasklet_node, "inp", rmemlet)
        state.add_edge(tasklet_node, "out", write_node, None, memlet)
        # dace.Memlet.from_array(name, write_node.desc(self.sdfg)))

    def _add_copy(self, target, operand):
        # value = self._parse_value(value_node)
        print(target, operand)

        state = self._add_state("CopyState")
        read_node = state.add_read(operand)
        write_node = state.add_write(target)
        state.add_nedge(
            read_node, write_node,
            dace.Memlet.from_array(target, self.sdfg.arrays[target]))

    def _add_mapped_copy(self, node, target, operand):
        # value = self._parse_value(value_node)
        print(target, operand)

        state = self._add_state("MappedCopyState")
        shape = self.sdfg.arrays[target].shape
        ranges = {
            "__idx_{}_{}_{}".format(node.lineno, node.col_offset, i):
            "0:{}".format(end)
            for i, end in enumerate(shape)
        }
        outputs = {"out": dace.Memlet.simple(target, ",".join(ranges.keys()))}
        if operand in self.sdfg.arrays:
            inputs = {
                "inp": dace.Memlet.from_array(operand,
                                              self.sdfg.arrays[operand])
            }
            code = "out = inp"
        else:
            inputs = {}
            code = "out = {}".format(operand)
        write_node = state.add_write(target)
        _, me, mx = state.add_mapped_tasklet(
            name="MappedCopy",
            map_ranges=ranges,
            inputs=inputs,
            outputs=outputs,
            code=code)
        if operand in self.sdfg.arrays:
            read_node = state.add_read(operand)
            state.add_nedge(
                read_node, me,
                dace.Memlet.from_array(operand, self.sdfg.arrays[operand]))
        state.add_nedge(
            mx, write_node,
            dace.Memlet.from_array(target, self.sdfg.arrays[target]))

    def _add_assignment(self, node, target, operand, op):
        print(target, operand, op)

        if isinstance(target, tuple):
            name, subset = target
            memlet = dace.Memlet(name, subset.num_elements(), subset, 1)
        else:
            name = target
            memlet = dace.Memlet.from_array(name, self.sdfg.arrays[name])
        if isinstance(operand, tuple):
            rname, rsubset = operand
        else:
            rname, rsubset = operand, None
        memlet.other_subset = rsubset
        if op is not None:
            memlet.wcr = LambdaProperty.from_string(
                'lambda x, y: x {} y'.format(op))

        state = self._add_state("assign_%d" % node.lineno)
        op1 = state.add_read(rname)
        op2 = state.add_write(name)
        state.add_nedge(op1, op2, memlet)

    def _get_variable_name(self, node, name):
        if name in self.variables:
            return self.variables[name]
        elif name in self.scope_vars:
            return self.scope_vars[name]
        else:
            raise DaceSyntaxError(self, node,
                                  'Array "%s" used before definition' % name)

    def visit_Assign(self, node: ast.Assign):

        self._visit_assign(node, node.targets[0], None)
        # Validate assignment targets
        # for target in _targets(node):
        #     if isinstance(target, ast.Name) and target.id in self.global_arrays:
        #         raise DaceSyntaxError(
        #             self, target,
        #             'Cannot reassign value to parameter "%s"' % target.id)
        #
        # if len(node.targets) > 1:
        #     raise DaceSyntaxError(
        #         self, node,
        #         "Only 1 target per assignment is currently supported")

    def _visit_assign(self, node, node_target, op):

        if not isinstance(node_target, ast.Tuple):
            elts = [node_target]
        else:
            elts = node_target.elts

        # state = self._add_state("assign_%d" % node.lineno)

        # Return result (UnaryOp, BinOp, BoolOp, Call, etc.)
        results = self.visit(node.value)
        if not isinstance(results, (list)):
            results = [results]
        if len(results) != len(elts):
            raise DaceSyntaxError(
                self, node,
                'Function returns %d values but %d provided' % (len(results),
                                                                len(elts)))

        for target, result in zip(elts, results):
            # Variable assignment
            if isinstance(target, ast.Name):
                if op is None:
                    if (target.id in self.scope_vars
                            or target.id in self.variables):
                        raise DaceSyntaxError(
                            self, target,
                            'Cannot reassign value to parameter "%s"' %
                            target.id)

                    name = target.id
                    self.variables[name] = result
                    continue
                else:
                    name = rname(target)
                    real_name = {**self.variables, **self.scope_vars}[name]
                    real_target = copy.deepcopy(target)
                    real_target.id = real_name
                    rng = dace.subsets.Range(
                        astutils.subscript_to_slice(real_target, {
                            **self.sdfg.arrays,
                            **self.scope_arrays
                        })[1])

            # Variable broadcast
            if isinstance(target, ast.Subscript):
                print(ast.dump(target))
                name = rname(target)
                real_name = {**self.variables, **self.scope_vars}[name]
                real_target = copy.deepcopy(target)
                real_target.value.id = real_name
                rng = dace.subsets.Range(
                    astutils.subscript_to_slice(real_target, {
                        **self.sdfg.arrays,
                        **self.scope_arrays
                    })[1])

            if self.nested:
                if (name, rng, 'w') in self.accesses:
                    self._add_tasklet(self.accesses[(name, rng, 'w')], result)
                elif name in self.variables:
                    aname = self.variables[name]
                    self._add_tasklet((aname, rng), result)
                elif ((name, rng, 'r') in self.accesses
                      or name in self.scope_vars):
                    vname = "__tmp_{l}_{c}".format(
                        l=target.lineno, c=target.col_offset)
                    parent_name = self.scope_vars[name]
                    parent_array = self.scope_arrays[parent_name]
                    sqz_rng = copy.deepcopy(rng)
                    sqz_rng.squeeze()
                    shape = sqz_rng.size()
                    dtype = parent_array.dtype
                    self.sdfg.add_array(
                        vname, shape, dtype, strides=sqz_rng.strides())
                    self.accesses[(name, rng, 'w')] = (vname, sqz_rng)
                    self.variables[vname] = parent_name
                    self.outputs[vname] = (dace.Memlet(parent_name,
                                                       rng.num_elements(), rng,
                                                       1), set())
                    if isinstance(result, tuple):
                        arr, subset = result
                    else:
                        arr, subset = result, None
                    if arr in self.sdfg.arrays:
                        if subset:
                            result_size = subset.num_elements()
                        else:
                            result_size = np.prod(self.sdfg.arrays[arr].shape)
                    else:
                        result_size = 1
                    if sqz_rng.num_elements() == result_size:
                        if sqz_rng.num_elements() == 1:
                            self._add_tasklet((vname, sqz_rng), result)
                        else:
                            # self._add_copy((vname, sqz_rng), result)
                            self._add_assignment(node, (vname, sqz_rng),
                                                 result, op)
                    else:
                        if result_size == 1:
                            self._add_mapped_copy(target, vname, result)
                        else:
                            raise DaceSyntaxError(self, node,
                                                  'Incompatible shapes')
                else:
                    raise DaceSyntaxError(
                        self, target,
                        'Array "{}" used before definition'.format(name))
            else:
                if name in self.variables:
                    aname = self.variables[name]
                    # self._add_tasklet((aname, rng), result)
                elif name in self.scope_vars:
                    aname = self.scope_vars[name]
                else:
                    raise DaceSyntaxError(
                        self, target,
                        'Array "{}" used before definition'.format(name))

                # self._add_tasklet((aname, rng), result)
                self._add_assignment(node, (aname, rng), result, op)

    def visit_AugAssign(self, node: ast.AugAssign):

        print(ast.dump(node))

        self._visit_assign(node, node.target,
                           augassign_ops[type(node.op).__name__])
        return

        state = self._add_state("AugAssignState")

        if isinstance(node.target, ast.Name):
            name = self._get_variable_name(node, node.target.id)
            output_node = state.add_write(name)
        else:
            raise NotImplementedError

        if isinstance(node.value, ast.Name):
            name = self._get_variable_name(node, node.value.id)
            input_node = state.add_read(name)
            constant = 1
        elif isinstance(node.value, ast.BinOp):
            if isinstance(node.value.left, ast.Name):
                name = self._get_variable_name(node, node.value.left.id)
                input_node = state.add_read(name)
            elif isinstance(node.value.right, ast.Name):
                name = self._get_variable_name(node, node.value.right.id)
                input_node = state.add_read(name)
            else:
                raise NotImplementedError
            if isinstance(node.value.left, ast.Num):
                constant = node.value.left.n
            elif isinstance(node.value.right, ast.Num):
                constant = node.value.right.n
            else:
                raise NotImplementedError

        if isinstance(node.op, ast.Add):
            pass
        elif isinstance(node.op, ast.Sub):
            constant = -constant
        else:
            raise NotImplementedError

        op_impl.constant_array_multiplication(
            state,
            constant,
            input_node,
            input_node,
            output_node,
            output_node,
            accumulate=True,
            label="AugAssign")

    def _get_keyword_value(self, keywords: List[ast.keyword], arg: str):
        """Finds a keyword in list and returns its value

        Arguments:
            keywords {List[ast.keyword]} -- Keyword list
            arg {str} -- Keyword ID

        Raises:
            DaceSyntaxError: If keyword is not found

        Returns:
            Any -- Keyword value
        """

        for kword in keywords:
            if kword.arg == arg:
                return kword.value

        raise DaceSyntaxError(self, keywords,
                              "Keyword {} not found".format(arg))

    def _parse_shape(self, node: Union[ast.List, ast.Tuple, ast.Attribute]):
        """Parses the shape of an array
        
        Arguments:
            node {Union[ast.List, ast.Tuple, ast.Attribute]} -- Shape node
        
        Raises:
            DaceSyntaxError: If shape node is ast.Attribute, but the attribute is not a shape
            DaceSyntaxError: If shape node is neither a list/tuple nor an attribute
        
        Returns:
            List[Union[str, int, dace.symbol]] -- Shape
        """

        if isinstance(node, (ast.List, ast.Tuple)):
            shape = []
            for length in node.elts:
                shape.append(self._parse_value(length))
        elif isinstance(node, ast.Attribute):
            if node.attr != "shape":
                raise DaceSyntaxError(
                    self, node, "Attribute {} is not shape".format(
                        rname(node)))
            shape = self.scope_arrays[node.value.id].shape
        else:
            raise DaceSyntaxError(
                self, node,
                "Array shape must either be a list of dimension lengths or "
                " the shape attribute of another array.")

        return shape

    def _parse_dtype(self, node: ast.Attribute):
        """Parses the dtype of an array
        
        Arguments:
            node {ast.Attribute} -- Dtype node
        
        Raises:
            DaceSyntaxError: If dtype node is an ast.Attribute, but the attribute is not a dtype
            DaceSyntaxError: If dtype node is not ast.Attribute
        
        Returns:
            Any -- Dtype
        """

        if isinstance(node, ast.Attribute):
            if node.value.id in {"dace", "numpy"}:
                dtype = getattr(self.globals[node.value.id], node.attr)
            elif node.attr != "dtype":
                raise DaceSyntaxError(
                    self, node, "Attribute {} is not dtype".format(
                        rname(node)))
            else:
                dtype = self.scope_arrays[node.value.id].dtype
        else:
            raise DaceSyntaxError(
                self, node, "Array dtype must either be a dace/numpy type or "
                " the dtype attribute of another array.")

        return dtype

    def _parse_ndarray(self, node: ast.Call):
        """Parses a call to numpy.ndarray
        
        Arguments:
            node {ast.Call} -- Call node
        
        Returns:
            Tuple[shape, dtype] -- Shape and dtype of the array
        """

        num_args = len(node.args)
        # num_kwargs = len(node.keywords)

        if num_args == 0:
            shape_node = self._get_keyword_value(node.keywords, "shape")
            shape = self._parse_shape(shape_node)
            print(shape)
            dtype_node = self._get_keyword_value(node.keywords, "dtype")
            dtype = self._parse_dtype(dtype_node)
            print(dtype)
        elif num_args == 1:
            shape_node = node.args[0]
            shape = self._parse_shape(shape_node)
            print(shape)
            dtype_node = self._get_keyword_value(node.keywords, "dtype")
            dtype = self._parse_dtype(dtype_node)
            print(dtype)
        elif num_args >= 2:
            shape_node = node.args[0]
            shape = self._parse_shape(shape_node)
            print(shape)
            dtype_node = node.args[1]
            dtype = self._parse_dtype(dtype_node)
            print(dtype)

        return (shape, dtype)

    def _parse_function_arg(self, arg: ast.AST):
        # Obtain a string representation
        return self.visit(arg)

    def _is_inputnode(self, sdfg: SDFG, name: str):
        for state in sdfg.nodes():
            for node in state.nodes():
                if isinstance(node, nodes.AccessNode) and node.data == name:
                    if state.out_degree(node) > 0:
                        return True

    def _is_outputnode(self, sdfg: SDFG, name: str):
        for state in sdfg.nodes():
            for node in state.nodes():
                if isinstance(node, nodes.AccessNode) and node.data == name:
                    if state.in_degree(node) > 0:
                        return True

    def visit_Call(self, node: ast.Call):
        default_impl = Config.get('frontend', 'implementation')
        funcname = rname(node)

        # If the function exists as a global SDFG or @dace.program, use it
        if funcname in self.other_sdfgs:
            from dace.frontend.python.parser import DaceProgram  # Avoiding import loop
            func = self.other_sdfgs[funcname]
            if isinstance(func, SDFG):
                sdfg = copy.deepcopy(func)
                args = [(arg.arg, self._parse_function_arg(arg.value))
                        for arg in node.keywords]
                # Validate argument types and sizes
                sdfg.argument_typecheck(
                    [], {k: self.sdfg.data(v)
                         for k, v in args},
                    types_only=True)
            elif isinstance(func, DaceProgram):
                args = [(aname, self._parse_function_arg(arg))
                        for aname, arg in zip(func.argnames, node.args)]

                sdfg = func.to_sdfg(*({
                    **self.defined,
                    **self.sdfg.arrays
                }[arg] if isinstance(arg, str) else arg
                                      for aname, arg in args))
            else:
                raise DaceSyntaxError(
                    self, node, 'Unrecognized SDFG type "%s" in call to "%s"' %
                    (type(func).__name__, funcname))

            state = self._add_state('call_%s_%d' % (funcname, node.lineno))
            argdict = {
                conn: Memlet.from_array(arg, self.sdfg.arrays[arg])
                for conn, arg in args if arg in self.sdfg.arrays
            }
            inputs = {
                k: v
                for k, v in argdict.items() if self._is_inputnode(sdfg, k)
            }
            outputs = {
                k: v
                for k, v in argdict.items() if self._is_outputnode(sdfg, k)
            }

            # Map internal SDFG symbols to external symbols (find_and_replace?)
            for aname, arg in args:
                if arg in self.sdfg.symbols or not isinstance(arg, str):
                    sdfg.replace(aname, arg)
                # Disallow memlets/nodes to symbol parameters
                elif aname in sdfg.symbols:
                    raise DaceSyntaxError(
                        self, node, 'Array nodes cannot be '
                        'passed as scalars to nested SDFG '
                        '(passing "%s" as "%s")' % (aname, arg))
            nsdfg = state.add_nested_sdfg(sdfg, self.sdfg, inputs.keys(),
                                          outputs.keys())
            self._add_dependencies(state, nsdfg, None, None, inputs, outputs)

            # No return values from SDFGs
            return []

        # TODO: If the function is a callback, implement it as a tasklet

        # Otherwise, try to find a default implementation for the SDFG
        func = oprepo.Replacements.get(funcname, default_impl)
        if func is None:
            # Check for SDFG as fallback
            func = oprepo.Replacements.get(funcname)
            if func is None:
                raise DaceSyntaxError(
                    self, node,
                    'Function "%s" is not registered with an SDFG implementation'
                    % funcname)
            print(
                'WARNING: Function "%s" is not registered with an %s implementation, falling back to SDFG'
                % (funcname, default_impl))

        args = [self._parse_function_arg(arg) for arg in node.args]
        keywords = {
            arg.arg: self._parse_function_arg(arg.value)
            for arg in node.keywords
        }

        self._add_state('call_%d' % node.lineno)

        result = func(self.sdfg, self.last_state, *args, **keywords)

        if not isinstance(result, (tuple, list)):
            return [result]
        return result

    # Used for memlet expressions outside of tasklets, otherwise ignored
    def visit_TopLevelExpr(self, node: ast.Expr):
        if isinstance(node.value, ast.BinOp):
            # Add two access nodes and a memlet (the arrays must already exist)
            if isinstance(node.value.op, ast.LShift):
                src = node.value.right
                dst = node.value.left
            elif isinstance(node.value.op, ast.RShift):
                src = node.value.left
                dst = node.value.right
            else:
                # Top-level binary operator that is not a memlet, does nothing
                self.generic_visit(node)
                return

            # Create an edge between the two data descriptors
            state = self._add_state('globalmemlet_%d' % node.lineno)
            src_expr = ParseMemlet(self, self.defined, src)
            dst_expr = ParseMemlet(self, self.defined, dst)
            src_name = src_expr.name
            if src_name not in self.sdfg.arrays:
                src_name = self.variables[src_expr.name]
            dst_name = dst_expr.name
            if dst_name not in self.sdfg.arrays:
                dst_name = self.variables[dst_expr.name]

            rnode = state.add_read(src_name)
            wnode = state.add_write(dst_name)
            state.add_nedge(
                rnode, wnode,
                Memlet(
                    src_name,
                    src_expr.accesses,
                    src_expr.subset,
                    1,
                    wcr=dst_expr.wcr,
                    wcr_identity=dst_expr.wcr_identity,
                    other_subset=dst_expr.subset))
            return

        # Calling reduction or other SDFGs / functions
        elif isinstance(node.value, ast.Call):
            # Handles reduction and calling other SDFGs / DaCe programs
            # self._add_state('call_%d' % node.lineno)
            self.visit_Call(node.value)
            return

        self.generic_visit(node)

    def visit_Return(self, node: ast.Return):
        if isinstance(node, (ast.Tuple, ast.List)):
            for elt in node.elts:
                # arrays['return'] = this
                pass
        else:
            pass
            # arrays['return'] = this
        pass

    def visit_With(self, node, is_async=False):
        # "with dace.tasklet" syntax
        if len(node.items) == 1:
            dec = node.items[0].context_expr
            funcname = rname(dec)
            if funcname == 'dace.tasklet':
                # Parse as tasklet
                state = self._add_state('with_%d' % node.lineno)
                tasklet, inputs, outputs, sdfg_inp, sdfg_out = self._parse_tasklet(
                    state, node)

                # Add memlets
                self._add_dependencies(state, tasklet, None, None, inputs,
                                       outputs)
                self.inputs.update(sdfg_inp)
                self.outputs.update(sdfg_out)
                return

        raise DaceSyntaxError(
            self, node,
            'General "with" statements disallowed in DaCe programs')

    def visit_AsyncWith(self, node):
        return self.visit_With(node, is_async=True)

    def _visitname(self, name: str, node: ast.AST):
        # First, if it is defined in the parser, use the definition
        if name in self.variables:
            return self.variables[name]

        # If an allowed global, use directly
        if name in self.globals:
            return _inner_eval_ast(self.globals, node)

        if name not in self.scope_vars:
            raise DaceSyntaxError(self, node,
                                  'Use of undefined variable "%s"' % name)
        return self.scope_vars[name]

    #### Visitors that return arrays
    def visit_Str(self, node: ast.Str):
        # A string constant returns itself
        return node.s

    def visit_Num(self, node: ast.Num):
        return node.n
        # name = None
        # if node.n not in self.numbers:
        #     dtype = None
        #     if isinstance(node.n, int):
        #         dtype = dace.int64
        #     elif isinstance(node.n, float):
        #         dtype = dace.float64
        #     elif isinstance(node.n, complex):
        #         dtype = dace.complex128
        #     else:
        #         raise NotImplementedError
        #     name, _ = self.sdfg.add_temp_transient([1], dtype, toplevel=True)
        #     self.numbers[node.n] = name
        #     init_state = None
        #     if not self.sdfg.nodes():
        #         init_state = self.sdfg.add_state('init')
        #         self.last_state = init_state
        #     else:
        #         init_state = self.sdfg.nodes()[0]
        #     tasklet = init_state.add_tasklet(
        #         'init_{}'.format(name), {}, {'out'}, 'out = {}'.format(node.n))
        #     access = init_state.add_write(name)
        #     init_state.add_edge(tasklet, 'out', access, None,
        #                         dace.Memlet.simple(name, '0'))
        # else:
        #     name = self.numbers[node.n]
        # return name

    def visit_Name(self, node: ast.Name):
        # If visiting a name, check if it is a defined variable or a global
        return self._visitname(node.id, node)

    def visit_Attribute(self, node: ast.Attribute):
        # If visiting an attribute, return attribute value if it's of an array or global
        name = until(astutils.unparse(node), '.')
        result = self._visitname(name, node)
        if result in self.sdfg.arrays:
            arr = self.sdfg.arrays[result]
        elif result in self.scope_arrays:
            arr = self.scope_arrays[result]
        else:
            return result
        try:
            return getattr(arr, node.attr)
        except KeyError:
            return result

    def visit_List(self, node: ast.List):
        # Recursively loop over elements
        return [self.visit(a) for a in node.elts]

    def visit_Tuple(self, node: ast.Tuple):
        # Recursively loop over elements
        return tuple(self.visit(a) for a in node.elts)

    def visit_Lambda(self, node: ast.Lambda):
        # Return a string representation of the function
        return astutils.unparse(node)

    ############################################################

    def _gettype(self, opnode: ast.AST):
        """ Returns an operand and its type as a 2-tuple of strings. """
        operand = self.visit(opnode)
        if isinstance(operand, (list, tuple)):
            if len(operand) != 1:
                raise DaceSyntaxError(self, opnode,
                                      'Operand cannot be a tuple')
            operand = operand[0]

        if isinstance(operand, str) and operand in self.sdfg.arrays:
            return operand, type(self.sdfg.arrays[operand]).__name__
        elif isinstance(operand, str) and operand in self.scope_arrays:
            return operand, type(self.scope_arrays[operand]).__name__
        else:
            return operand, type(operand).__name__
        # if isinstance(operand, str):
        #     if operand in self.sdfg.arrays:
        #         return operand, type(self.sdfg.arrays[operand]).__name__
        #     elif operand in self.sdfg.symbols:
        #         return operand, data.Scalar.__name__
        # elif dtypes.isconstant(operand):
        #     return operand, data.Scalar.__name__
        # return operand, type(operand).__name__
    
    def _convert_num_to_array(self, node: ast.Num):
        name = None
        if node.n not in self.numbers:
            dtype = None
            if isinstance(node.n, int):
                dtype = dace.int64
            elif isinstance(node.n, float):
                dtype = dace.float64
            elif isinstance(node.n, complex):
                dtype = dace.complex128
            else:
                raise NotImplementedError
            name, _ = self.sdfg.add_temp_transient([1], dtype, toplevel=True)
            self.numbers[node.n] = name
            init_state = None
            if not self.sdfg.nodes():
                init_state = self.sdfg.add_state('init')
                self.last_state = init_state
            else:
                init_state = self.sdfg.nodes()[0]
            tasklet = init_state.add_tasklet(
                'init_{}'.format(name), {}, {'out'}, 'out = {}'.format(node.n))
            access = init_state.add_write(name)
            init_state.add_edge(tasklet, 'out', access, None,
                                dace.Memlet.simple(name, '0'))
        else:
            name = self.numbers[node.n]
        return name

    def _visit_op(self, node: Union[ast.UnaryOp, ast.BinOp, ast.BoolOp],
                  op1: ast.AST, op2: ast.AST):
        default_impl = Config.get('frontend', 'implementation')
        opname = None
        try:
            opname = type(node.op).__name__
        except:
            pass

        # Parse operands
        operand1, op1type = self._gettype(op1)
        if op2 is not None:
            operand2, op2type = self._gettype(op2)
        else:
            operand2, op2type = None, None

        if isinstance(node, ast.BinOp):
            if op1type == 'Array' and isinstance(op2, ast.Num):
                operand2 = self._convert_num_to_array(op2)
                op2type = 'Array'
            elif op2type == 'Array' and isinstance(op1, ast.Num):
                operand1 = self._convert_num_to_array(op1)
                op1type = 'Array'

        func = oprepo.Replacements.getop(
            op1type, opname, implementation=default_impl, otherclass=op2type)
        if func is None:
            # Check for SDFG as fallback
            func = oprepo.Replacements.getop(
                op1type, opname, otherclass=op2type)
            if func is None:
                raise DaceSyntaxError(
                    self, node,
                    'Operator "%s" is not defined for types %s and %s' %
                    (opname, op1type, op2type))
            print(
                'WARNING: Operator "%s" is not registered with an %s implementation for'
                'types %s and %s, falling back to SDFG' %
                (opname, default_impl, op1type, op2type))

        self._add_state('%s_%d' % (type(node).__name__, node.lineno))
        result = func(self, self.sdfg, self.last_state, operand1, operand2)
        if not isinstance(result, (tuple, list)):
            return [result]
        return result

    def visit_UnaryOp(self, node: ast.UnaryOp):
        return self._visit_op(node, node.operand, None)

    def visit_BinOp(self, node: ast.BinOp):
        return self._visit_op(node, node.left, node.right)

    def visit_BoolOp(self, node: ast.BoolOp):
        last = node.values[0]
        # Syntax of BoolOp is a list of values, we parse left to right
        for i in range(1, len(node.values)):
            last = self._visit_op(node, last, node.values[i])
        return last

    def _add_read_access(self, name, rng, target):
        if (name, rng, 'w') in self.accesses:
            return self.accesses[(name, rng, 'w')]
        elif (name, rng, 'r') in self.accesses:
            return self.accesses[(name, rng, 'r')]
        elif name in self.variables:
            return (name, rng)
        elif name in self.scope_vars:
            if target:
                vname = "__tmp_{l}_{c}".format(
                    l=target.lineno, c=target.col_offset)
            else:
                pname = "__tmp_{n}_".format(n=name)
                i = 0
                while pname + str(i) in {
                        **self.sdfg.arrays,
                        **self.scope_arrays
                }:
                    i += 1
                vname = pname + str(i)
            self.accesses[(name, rng, 'r')] = vname
            parent_name = self.scope_vars[name]
            parent_array = self.scope_arrays[parent_name]
            sqz_rng = copy.deepcopy(rng)
            sqz_rng.squeeze()
            shape = sqz_rng.size()
            dtype = parent_array.dtype
            self.sdfg.add_array(vname, shape, dtype, strides=sqz_rng.strides())
            self.accesses[(name, rng, 'r')] = (vname, sqz_rng)
            self.inputs[vname] = (dace.Memlet(parent_name, rng.num_elements(),
                                              rng, 1), set())
            return (vname, sqz_rng)

    ### Subscript (slicing) handling
    def visit_Subscript(self, node: ast.Subscript):
        if self.nested:
            target = node
            name = rname(target)
            real_name = {**self.variables, **self.scope_vars}[name]
            real_target = copy.deepcopy(target)
            real_target.value.id = real_name
            rng = dace.subsets.Range(
                astutils.subscript_to_slice(real_target, {
                    **self.sdfg.arrays,
                    **self.scope_arrays
                })[1])
            if (name, rng, 'w') in self.accesses:
                return self.accesses[(name, rng, 'w')]
            elif (name, rng, 'r') in self.accesses:
                return self.accesses[(name, rng, 'r')]
            elif name in self.variables:
                return (name, rng)
            elif name in self.scope_vars:
                vname = "__tmp_{l}_{c}".format(
                    l=target.lineno, c=target.col_offset)
                self.accesses[(name, rng, 'r')] = vname
                parent_name = self.scope_vars[name]
                parent_array = self.scope_arrays[parent_name]
                sqz_rng = copy.deepcopy(rng)
                sqz_rng.squeeze()
                shape = sqz_rng.size()
                dtype = parent_array.dtype
                self.sdfg.add_array(
                    vname, shape, dtype, strides=sqz_rng.strides())
                self.accesses[(name, rng, 'r')] = (vname, sqz_rng)
                self.inputs[vname] = (dace.Memlet(parent_name,
                                                  rng.num_elements(), rng, 1),
                                      set())
                # return (vname, sqz_rng)
                return vname
            else:
                raise DaceSyntaxError(
                    self, target,
                    'Array "{}" used before definition'.format(name))
        else:
            # Obtain array
            array, arrtype = self._gettype(node.value)
            if arrtype == 'str' or arrtype in dtypes._CTYPES:
                raise DaceSyntaxError(self, node,
                                      'Type "%s" cannot be sliced' % arrtype)

            # Try to construct memlet from subscript
            # expr: MemletExpr = ParseMemlet(self, self.defined, node)
            # TODO: This needs to be formalized better
            node.value.id = array
            expr: MemletExpr = ParseMemlet(self, self.sdfg.arrays, node)
            arrobj = self.sdfg.arrays[array]

            # TODO: Check dimensionality of access and extend as necessary

            # Add slicing state
            self._add_state('slice_%s_%d' % (array, node.lineno))
            other_subset = copy.deepcopy(expr.subset)
            other_subset.squeeze()
            tmp, tmparr = self.sdfg.add_temp_transient(
                other_subset.size(), arrobj.dtype, arrobj.storage)
            rnode = self.last_state.add_read(array)
            wnode = self.last_state.add_write(tmp)
            self.last_state.add_nedge(
                rnode, wnode,
                Memlet(array, expr.accesses, expr.subset, 1, expr.wcr,
                       expr.wcr_identity, other_subset))
            return tmp

    ##################################
