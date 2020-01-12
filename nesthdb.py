#!/usr/bin/python3
# -*- coding: future_fstrings -*-
import importlib
import logging
import sys
import tempfile

from collections import defaultdict

from common import *
from dpdb.abstraction import MinorGraph, ClingoControl
from dpdb.db import BlockingThreadedConnectionPool, DEBUG_SQL, setup_debug_sql
from dpdb.problems.nestpmc import NestPmc
from dpdb.problems.sat_util import *
from dpdb.reader import CnfReader
from dpdb.writer import FileWriter, StreamWriter, denormalize_cnf, normalize_cnf

logger = logging.getLogger("nestHDB")
#setup_logging("DEBUG")
setup_logging()
setup_debug_sql()

class Formula:
    def __init__(self, vars, clauses, projected=None):
        self.vars = vars
        self.num_vars = len(vars)
        self.clauses = clauses
        self.num_clauses = len(clauses)
        self.projected = projected
        self.var_clause_dict = defaultdict(set)

    @classmethod
    def from_file(cls, fname):
        input = CnfReader.from_file(fname)
        return cls(input.vars, input.clauses, input.projected)

class Graph:
    def __init__(self, nodes, edges, adj_list):
        self.nodes = nodes
        self.edges = edges
        self.adj_list = adj_list
        self.tree_decomp = None

    @property
    def num_nodes(self):
        return len(self.nodes)

    @property
    def num_edges(self):
        return len(self.edges)

    def abstract(self, non_nested):
        proj_out = self.nodes - non_nested
        mg = MinorGraph(self.nodes, self.adj_list, proj_out)
        mg.abstract()
        mg.add_cliques()
        self.nodes = mg.nodes
        self.edges = mg.edges
        self.adj_list = mg.adj_list
        self.mg = mg

    def normalize(self):
        self.nodes_normalized = set()
        self.edges_normalized = set()
        self.adj_list_normalized = {}
        self._node_map = {}
        self._node_rev_map = {}

        last = 0
        for n in self.nodes:
            last += 1
            self._node_map[n] = last
            self._node_rev_map[last] = n
            self.nodes_normalized.add(last)

        for e in self.edges:
            u = self._node_map[e[0]]
            v = self._node_map[e[1]]
            if u < v:
                self.edges_normalized.add((u,v))
            else:
                self.edges_normalized.add((v,u))

    def decompose(self, seed=42):
        global cfg
        self.normalize()
        self.tree_decomp = decompose(self.num_nodes,self.edges_normalized,cfg["htd"],gr_file="test.gr",td_file="test.td",node_map=self._node_rev_map)

class Problem:
    def __init__(self, formula, non_nested, depth=0):
        self.formula = formula
        self.projected = formula.projected
        self.projected_orig = formula.projected
        self.non_nested = non_nested
        self.non_nested_orig = non_nested
        self.maybe_sat = True
        self.models = None
        self.depth = depth

    def preprocess(self):
        global cfg
        if "preprocessor" not in cfg["nesthdb"]:
            return # True, num_vars, vars, len(clauses), clauses, None
        cfg_prep = cfg["nesthdb"]["preprocessor"]
        preprocessor = [cfg_prep["path"]]
        if "args" in cfg_prep:
            preprocessor.extend(cfg_prep["args"].split(' '))
        ppmc = subprocess.Popen(preprocessor,stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        clauses,proj_vars,num_vars,mapping,rev_mapping = normalize_cnf(self.formula.clauses, None, True)
        StreamWriter(ppmc.stdin).write_cnf(num_vars,clauses,normalize=False)
        ppmc.stdin.close()
        input = CnfReader.from_stream(ppmc.stdout,silent=True)
        ppmc.wait()
        ppmc.stdout.close()
        if not input.error:
            self.maybe_sat = input.maybe_sat
            if not input.done:
                clauses, vars = denormalize_cnf(input.clauses,input.vars,rev_mapping)
                self.formula = Formula(vars,clauses)
                self.projected = self.projected.intersection(input.vars)
            elif len(self.projected) == 0 or self.projected.intersection(self.formula.vars) == self.projected:
                # use result if instance was sat or #sat; result for pmc would be wrong
                self.models = input.models
        else:
            logger.debug("Pre-processor failed... ignoring result")

    def decompose_nested_primal(self):
        num_vars, edges, adj = cnf2primal(self.formula.num_vars, self.formula.clauses, self.formula.var_clause_dict, True)
        self.graph = Graph(set(self.formula.vars), edges, adj)
        logger.info(f"Primal graph #vertices: {num_vars}, #edges: {len(edges)}")
        #nodes, normalized_adj, normalized_edges, mg = abstract(vars, adj, self.projected)
        self.graph.abstract(self.non_nested)
        logger.info(f"Nested primal graph #vertices: {self.graph.num_nodes}, #edges: {self.graph.num_edges}")
        self.graph.decompose()

    def choose_subset(self):
        global cfg
        cfg_asp = cfg["nesthdb"]["asp"]
        for enc in cfg_asp["encodings"]:
            size = enc["size"]
            timeout = 30 if "timeout" not in enc else enc["timeout"]
            logger.debug("Running clingo %s for size %d and timeout %d", enc["file"],size,timeout)
            c = ClingoControl(self.graph.edges,self.non_nested)
            res = c.choose_subset(min(size,len(self.non_nested)),enc["file"],timeout)[2]
            if len(res) == 0:
                logger.warning("Clingo did not produce an answer set, fallback to previous result {}".format(projected))
            else:
                self.non_nested = set(res[0])
            logger.debug("Clingo done%s", " (timeout)" if c.timeout else "")

    def call_solver(self,type):
        global cfg

        logger.info(f"Call solver: {type} with #vars {self.formula.num_vars}, #clauses {len(self.formula.clauses)}, #projected {len(self.projected)}")

        cfg_str = f"{type}_solver"
        assert(cfg_str in cfg["nesthdb"])
        assert("path" in cfg["nesthdb"][cfg_str])
        local_cfg = cfg["nesthdb"][cfg_str]
        solver = [local_cfg["path"]]

        if "seed_arg" in local_cfg:
            solver.append(local_cfg["seed_arg"])
            # TODO: enable once we use real arguments and have a real seed... 
            #solver.append(str(kwargs["runid"]))
            solver.append("42")
        if "args" in local_cfg:
            solver.extend(local_cfg["args"].split(' '))
        if "output_parser" in local_cfg:
            solver_parser = local_cfg["output_parser"]
            reader_module = importlib.import_module("dpdb.reader")
            solver_parser_cls = getattr(reader_module, solver_parser["class"])
        else:
            solver_parser = {"class":"CnfReader","args":{"silent":True},"result":"models"}
            solver_parser_cls = CnfReader

        tmp = tempfile.NamedTemporaryFile().name
        with FileWriter(tmp) as fw:
            fw.write_cnf(self.formula.num_vars,self.formula.clauses,normalize=True, proj_vars=self.projected)
            for i in range(0,128,1):
                psat = subprocess.Popen(solver + [tmp], stdout=subprocess.PIPE)
                output = solver_parser_cls.from_stream(psat.stdout,**solver_parser["args"])
                psat.wait()
                psat.stdout.close()
                result = getattr(output,solver_parser["result"])
                if psat.returncode == 245 or psat.returncode == 250:
                    logger.debug("Retrying call to external solver, returncode {}, index {}".format(psat.returncode, i))
                else:
                    logger.debug("No Retry, returncode {}, result {}, index {}".format(psat.returncode, result, i))
                    break

        logger.info(f"Solver {type} result: {result}")
        return result
    
    def solve_classic(self):
        if self.formula.vars == self.projected:
            return self.call_solver("sharpsat")
        else:
            return self.call_solver("pmc")

    def nestedpmc(self):
        global cfg

        pool = BlockingThreadedConnectionPool(1,cfg["db"]["max_connections"],**cfg["db"]["dsn"])
        #problem_cfg = {}
        #if "problem_specific" in cfg and cls.__name__.lower() in cfg["problem_specific"]:
        #    problem_cfg = cfg["problem_specific"][cls.__name__.lower()]
        #problem = NestPmc(file,pool, **cfg["dpdb"], **flatten_cfg(problem_cfg, [], '_',cls.keep_cfg()), **kwargs)
        problem = NestPmc("test",pool, **cfg["dpdb"])
        problem.set_td(self.graph.tree_decomp)
        problem.set_recursive(self.solve_rec,self.depth)
        problem.set_input(self.graph.num_nodes,-1,self.projected,self.non_nested_orig,self.formula.var_clause_dict,self.graph.mg)
        problem.setup()
        problem.solve()
        return problem.model_count

    def solve(self):
        logger.info(f"Original #vars: {self.formula.num_vars}, #clauses: {self.formula.num_clauses}, #projected: {len(self.projected_orig)}, depth: {self.depth}")
        self.preprocess()
        if self.maybe_sat == False:
            logger.info("Preprocessor UNSAT")
            return 0
        if self.models != None:
            logger.info(f"Solved by preprocessor: {self.models} models")
            return self.models

        self.non_nested = self.non_nested.intersection(self.projected)
        logger.info(f"Preprocessing #vars: {self.formula.num_vars}, #clauses: {self.formula.num_clauses}, #projected: {len(self.projected)}")

        if len(self.projected.intersection(self.formula.vars)) == 0:
            logger.info("Intersection of vars and projected is empty")
            return self.call_solver("sat")

        self.decompose_nested_primal()

        if self.depth > cfg["nesthdb"]["max_recursion_depth"] or self.graph.tree_decomp.tree_width >= cfg["nesthdb"]["threshold_hybrid"]: #TODO OR PROJECTION SIZE BELOW TRESHOLD OR CLAUSE SIZE BELOW TRESHOLD
            logger.info("Tree width >= hybrid threshold ({})".format(cfg["nesthdb"]["threshold_hybrid"]))
            return self.solve_classic()

        if self.graph.tree_decomp.tree_width >= cfg["nesthdb"]["threshold_abstract"]:
            logger.info("Tree width >= abstract threshold ({})".format(cfg["nesthdb"]["threshold_abstract"]))
            self.choose_subset()
            logger.info(f"Subset #non-nested: {len(self.non_nested)}")
            self.decompose_nested_primal()
            if self.graph.tree_decomp.tree_width >= cfg["nesthdb"]["threshold_abstract"]:
                logger.info("Tree width after abstraction >= abstract threshold ({})".format(cfg["nesthdb"]["threshold_abstract"]))
                return self.solve_classic()

        result = self.nestedpmc()
        return result * int(2**(len(self.projected_orig)-len(self.projected)))

    def solve_rec(self, vars, clauses, non_nested, projected, depth=0):
        return Problem(Formula(vars,clauses,projected),non_nested,depth).solve()

def read_input(fname):
    input = CnfReader.from_file(fname)
    return input.num_vars, input.vars, input.num_clauses, input.clauses, input.projected

def main():
    global cfg
    cfg = read_cfg("config.json")
    fname = sys.argv[1]

    formula = Formula.from_file(fname)
    prob = Problem(formula,formula.vars)
    print("result: ",prob.solve())

if __name__ == "__main__":
    main()
