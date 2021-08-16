# Copyright 2019-2021 ETH Zurich and the DaCe authors. All rights reserved.
""" Contains classes that fuse Tasklets """

from typing import Any, Dict, Set
import ast
import dace
import re
from dace import dtypes, registry
from dace.sdfg import nodes
from dace.sdfg import utils as sdutil
from dace.transformation import transformation as pm


class ConnectorRenamer(ast.NodeTransformer):
    """ Renames connector names in Tasklet code.
    """
    def __init__(self, repl_dict: Dict[str, str]) -> None:
        """ Initializes AST transformer.
            :param repl_dict: Replacement dictionary.
        """
        self.repl_dict = repl_dict

    def visit_Name(self, node: ast.Name) -> Any:
        # Rename connector
        if node.id in self.repl_dict:
            node.id = self.repl_dict[node.id]
        return self.generic_visit(node)


class PythonRHSExtractor(ast.NodeVisitor):
    """ Extracts assignments' RHS in Tasklet code.
    """
    def __init__(self):
        self.assignments = set()

    def visit_Assign(self, node: ast.Assign):
        if isinstance(node.targets[0], ast.Name):
            self.assignments.add(node.targets[0].id)

    def visit_AugAssign(self, node: ast.AugAssign):
        if isinstance(node.targets[0], ast.Name):
            self.assignments.add(node.targets[0].id)


@registry.autoregister_params(singlestate=True, strict=False)
class SimpleTaskletFusion(pm.Transformation):
    """ Fuses two connected Tasklets.
        It is recommended that this transformation is used on Tasklets that
        contain only simple assignments.
    """

    _t1 = pm.PatternNode(nodes.Tasklet)
    _t2 = pm.PatternNode(nodes.Tasklet)

    @staticmethod
    def expressions():
        return [
            sdutil.node_path_graph(SimpleTaskletFusion._t1,
                                   SimpleTaskletFusion._t2)
        ]

    @staticmethod
    def can_be_applied(graph: dace.SDFGState,
                       candidate: Dict[pm.PatternNode, int],
                       expr_index: int,
                       sdfg: dace.SDFG,
                       strict: bool = False):

        t1 = graph.node(candidate[SimpleTaskletFusion._t1])
        t2 = graph.node(candidate[SimpleTaskletFusion._t2])

        # Tasklets must be of the same language
        if t1.language != t2.language:
            return False

        # Avoid circles
        t1_dst = set()
        for e in graph.out_edges(t1):
            t1_dst.add(e.dst)
        t2_src = set()
        for e in graph.in_edges(t2):
            t2_src.add(e.src)
        if len(t1_dst.intersection(t2_src)):
            return False

        return True

    @staticmethod
    def match_to_str(graph: dace.SDFGState, candidate: Dict[pm.PatternNode,
                                                            int]) -> str:
        t1 = graph.node(candidate[SimpleTaskletFusion._t1])
        t2 = graph.node(candidate[SimpleTaskletFusion._t2])
        return f'fuse({t1.label}, {t2.label})'

    def apply(self, sdfg: dace.SDFG):
        graph = sdfg.nodes()[self.state_id]
        t1 = graph.nodes()[self.subgraph[self._t1]]
        t2 = graph.nodes()[self.subgraph[self._t2]]

        def rename_conn(conn: str, names: Set[str]) -> str:
            """ Renames connector so that it doesn't clash with names.
            """
            match = re.match('(.*?)([0-9]+)$', conn)
            if match:
                pre = match.group(1)
            else:
                pre = f'{conn}_'
            i = 0
            while f'{pre}{i}' in names:
                i += 1
            return f'{pre}{i}'

        def replace(tasklet, repl_dict):
            """ Renames connectors based on the input replacement dictionary.
            """
            if tasklet.language is dtypes.Language.Python:
                repl = ConnectorRenamer(repl_dict)
                for stmt in tasklet.code.code:
                    repl.visit(stmt)
            elif tasklet.language is dtypes.Language.CPP:
                for old, new in repl_dict.items():
                    tasklet.code.code = re.sub(r'\b%s\b' % re.escape(old), new,
                                               tasklet.code.as_string)

        def replace_rhs(tasklet, repl_dict):
            """ Replaces assignments' RHS based on the input replacement
                dictionary. This is used only on CPP tasklets.
            """
            if tasklet.language is dtypes.Language.Python:
                raise ValueError(
                    "This method should only be used with CPP Tasklets")
            elif tasklet.language is dtypes.Language.CPP:
                for old, new in repl_dict.items():
                    tasklet.code.code = re.sub(
                        r'(?<!auto\s)%s[\s\t]*=' % re.escape(old), new,
                        tasklet.code.as_string)

        def extract_rhs(tasklet) -> Set[str]:
            """ Returns the RHS of assignments in Tasklet code.
            """
            if tasklet.language is dtypes.Language.Python:
                extr = PythonRHSExtractor()
                for stmt in tasklet.code.code:
                    extr.visit(stmt)
                return extr.assignments
            elif tasklet.language is dtypes.Language.CPP:
                rhs = set()
                for match in re.findall('[\s\t\n\r]*([\w]*)[\s\t]*=',
                                        tasklet.code.code):
                    rhs.add(match)
                return rhs

        rdict = dict()
        rdict_inout = dict()

        # Find names of current and former connectors
        # (assignments' RHS that are not connectors).
        t1_names = t1.in_connectors.keys() | t1.out_connectors.keys()
        t1_rhs = extract_rhs(t1)
        if t1_rhs:
            t1_names |= t1_rhs
        t2_names = t2.in_connectors.keys() | t2.out_connectors.keys()
        t2_rhs = extract_rhs(t2)
        if t2_rhs:
            t2_names |= t2_rhs

        # Change t2 connector names.
        nlist = list(t2_names)
        for name in nlist:
            if name in t1_names:
                newname = rename_conn(name, t1_names | t2_names)
                rdict[name] = newname
                t2_names.remove(name)
                t2_names.add(newname)
        if rdict:
            replace(t2, rdict)

        # Handle input edges.
        inconn = {}
        for e in graph.in_edges(t1):
            inconn[e.dst_conn] = t1.in_connectors[e.dst_conn]
        for e in graph.in_edges(t2):
            graph.remove_edge(e)
            conn = e.dst_conn
            if conn in rdict.keys():
                conn = rdict[conn]
            if e.src is t1:
                rdict_inout[conn] = e.src_conn
            else:
                inconn[conn] = t2.in_connectors[e.dst_conn]
                graph.add_edge(e.src, e.src_conn, t1, conn, e.data)

        # Handle output edges.
        outconn = {}
        for e in graph.out_edges(t1):
            outconn[e.src_conn] = t1.out_connectors[e.src_conn]
        for e in graph.out_edges(t2):
            graph.remove_edge(e)
            conn = e.src_conn
            if conn in rdict:
                conn = rdict[conn]
            outconn[conn] = t2.out_connectors[e.src_conn]
            graph.add_edge(t1, conn, e.dst, e.dst_conn, e.data)

        # Rename in-out connectors.
        if rdict_inout:
            replace(t2, rdict_inout)

        # Update t1 connectors and code.
        t1.in_connectors = inconn
        t1.out_connectors = outconn
        if t1.language is dtypes.Language.Python:
            t1.code.code.extend(t2.code.code)
        elif t1.language is dtypes.Language.CPP:
            t1.code.code += f'\n{t2.code.code}'
        graph.remove_node(t2)

        # Fix CPP assignemnt RHS that are not connectors.
        if t1.language is dtypes.Language.CPP:
            rhs = extract_rhs(t1)
            repl_dict = dict()
            for name in rhs:
                if name not in inconn and name not in outconn:
                    repl_dict[name] = f'auto {name} ='
            if repl_dict:
                replace_rhs(t1, repl_dict)
