import logging
import webbrowser
from copy import deepcopy
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

import matplotlib
import networkx as nx
import numpy as np
import pandas as pd
import plotly.graph_objects as go
from matplotlib import pyplot as plt
from matplotlib.colors import ListedColormap, LinearSegmentedColormap
import matplotlib.cm as cm
import seaborn as sns
from networkx import cytoscape_data

from plotly.subplots import make_subplots

from elphick.mass_composition import MassComposition
from elphick.mass_composition.config.config_read import read_flowsheet_yaml
from elphick.mass_composition.dag import DAG
from elphick.mass_composition.layout import digraph_linear_layout
from elphick.mass_composition.mc_node import MCNode, NodeType
from elphick.mass_composition.plot import parallel_plot, comparison_plot
from elphick.mass_composition.stream import Stream
from elphick.mass_composition.utils.geometry import midpoint
from elphick.mass_composition.utils.loader import streams_from_dataframe
from elphick.mass_composition.utils.sampling import random_int


class Flowsheet:
    def __init__(self, name: str = 'Flowsheet'):
        self.name: str = name
        self.graph: nx.DiGraph = nx.DiGraph()
        self._logger: logging.Logger = logging.getLogger(__class__.__name__)

    @classmethod
    def from_streams(cls, streams: List[Union[Stream, MassComposition]],
                     name: Optional[str] = 'Flowsheet') -> 'Flowsheet':
        """Instantiate from a list of objects

        Args:
            streams: List of MassComposition objects
            name: name of the network

        Returns:

        """

        streams: List[Union[Stream, MassComposition]] = cls._check_indexes(streams)
        bunch_of_edges: List = []
        for stream in streams:
            if stream._nodes is None:
                raise KeyError(f'Stream {stream.name} does not have the node property set')
            nodes = stream._nodes

            # add the objects to the edges
            bunch_of_edges.append((nodes[0], nodes[1], {'mc': stream}))

        graph = nx.DiGraph(name=name)
        graph.add_edges_from(bunch_of_edges)
        d_node_objects: Dict = {}
        for node in graph.nodes:
            d_node_objects[node] = MCNode(node_id=int(node))
        nx.set_node_attributes(graph, d_node_objects, 'mc')

        for node in graph.nodes:
            d_node_objects[node].inputs = [graph.get_edge_data(e[0], e[1])['mc'] for e in graph.in_edges(node)]
            d_node_objects[node].outputs = [graph.get_edge_data(e[0], e[1])['mc'] for e in graph.out_edges(node)]

        graph = nx.convert_node_labels_to_integers(graph)
        # update the temporary nodes on the mc object property to match the renumbered integers
        for node1, node2, data in graph.edges(data=True):
            data['mc'].nodes = [node1, node2]
        obj = cls()
        obj.graph = graph
        return obj

    @classmethod
    def from_dataframe(cls, df: pd.DataFrame,
                       name: Optional[str] = 'Flowsheet',
                       mc_name_col: Optional[str] = None,
                       n_jobs: int = 1) -> 'Flowsheet':
        """Instantiate from a DataFrame

        Args:
            df: The DataFrame
            name: name of the network
            mc_name_col: The column specified contains the names of objects to create.
              If None the DataFrame is assumed to be wide and the mc objects will be extracted from column prefixes.
            n_jobs: The number of parallel jobs to run.  If -1, will use all available cores.

        Returns:
            Flowsheet: An instance of the Flowsheet class initialized from the provided DataFrame.

        """
        streams: Dict[Union[int, str], MassComposition] = streams_from_dataframe(df=df, mc_name_col=mc_name_col,
                                                                                 n_jobs=n_jobs)
        return cls().from_streams(streams=list(streams.values()), name=name)

    @classmethod
    def from_yaml(cls, flowsheet_file: Path) -> 'Flowsheet':
        """Construct a flowsheet defined in a yaml file

        Args:
            flowsheet_file: The yaml file following the prescribed format

        Returns:

        """
        config = read_flowsheet_yaml(flowsheet_file)
        obj = cls(name=config['flowsheet']['name'])

        bunch_of_edges: List = []
        for stream, nodes in config['streams'].items():
            # add the objects to the edges
            bunch_of_edges.append(
                (nodes['node_in'], nodes['node_out'],
                 {'mc': MassComposition(name=stream, data=pd.DataFrame(columns=['mass_wet', 'mass_dry', 'H2O']))}))

        graph = nx.DiGraph(name=config['flowsheet']['name'])
        graph.add_edges_from(bunch_of_edges)

        d_node_objects: Dict = {}
        for node in graph.nodes:
            d_node_objects[node] = MCNode(node_id=int(node), node_name=config['nodes'][node]['name'],
                                          node_subset=config['nodes'][node]['subset'])
        nx.set_node_attributes(graph, d_node_objects, 'mc')

        obj.graph = graph
        return obj

    @classmethod
    def from_dag(cls, dag: DAG) -> 'Flowsheet':
        """Construct a flowsheet from a dag object

        Args:
            dag: The dag object that has been run previously.

        Returns:

        """

        # Create a new instance of Flowsheet
        fs = cls(name=dag.name)

        # Copy the nodes from the dag to the Flowsheet
        for nid, (node, data) in enumerate(dag.graph.nodes(data=True)):
            fs.graph.add_node(node, mc=MCNode(node_id=nid, node_name=node))

        # Copy the edges from the dag to the Flowsheet
        for edge in dag.graph.edges:
            # Use the name of the MassComposition object as the name of the edge
            fs.graph.add_edge(*edge, **dag.graph.edges[edge])

        # Populate the inputs and outputs properties of the MCNode objects
        for node in fs.graph.nodes:
            mc_node = fs.graph.nodes[node]['mc']
            mc_node.inputs = [fs.graph.edges[edge]['mc'] for edge in fs.graph.in_edges(node)]
            mc_node.outputs = [fs.graph.edges[edge]['mc'] for edge in fs.graph.out_edges(node)]

        return fs

    def to_simple(self, node_name: Optional[str] = None) -> 'Flowsheet':
        """Return the simplified flowsheet"""

        node_name = node_name if node_name is not None else self.name

        # Identify the degree-1 nodes
        degree_one_nodes = [node for node, degree in self.graph.degree() if degree == 1]

        # Create a subgraph that only includes the degree-1 nodes and their edges
        subgraph = self.graph.subgraph(degree_one_nodes).copy()

        # Create a new node that represents the "system-internals"
        system_node = max(self.graph.nodes) + 1  # Ensure the new node has a unique identifier
        subgraph.add_node(system_node, mc=MCNode(node_id=system_node, node_name=node_name))

        # Connect the degree-one nodes to the "system-internals" node
        for node in degree_one_nodes:
            # For in-edges, connect the node to the "system-internals" node
            for edge in self.graph.in_edges(node, data=True):
                subgraph.add_edge(system_node, node, **edge[2])

            # For out-edges, connect the "system-internals" node to the node
            for edge in self.graph.out_edges(node, data=True):
                subgraph.add_edge(node, system_node, **edge[2])

        # Populate the inputs and outputs properties of the MCNode objects
        for node in subgraph.nodes:
            mc_node = subgraph.nodes[node]['mc']
            mc_node.inputs = [subgraph.edges[edge]['mc'] for edge in subgraph.in_edges(node)]
            mc_node.outputs = [subgraph.edges[edge]['mc'] for edge in subgraph.out_edges(node)]

        # Create a new Flowsheet from the subgraph
        fs = self.__class__(name=self.name)
        fs.graph = subgraph

        return fs

    @property
    def balanced(self) -> bool:
        bal_vals: List = [self.graph.nodes[n]['mc'].balanced for n in self.graph.nodes]
        bal_vals = [bv for bv in bal_vals if bv is not None]
        return all(bal_vals)

    @property
    def edge_status(self) -> Tuple:
        d_edge_status_ok: Dict = {}
        d_failing_edges: Dict = {}
        for u, v, data in self.graph.edges(data=True):
            d_edge_status_ok[data['mc'].name] = data['mc'].status.ok
            if not data['mc'].status.ok:
                d_failing_edges[data['mc'].name] = data['mc'].status.failing_components
        return all(d_edge_status_ok.values()), d_failing_edges

    def to_json(self) -> Dict:
        json_graph: Dict = cytoscape_data(self.graph)
        return json_graph

    def get_edge_by_name(self, name: str) -> MassComposition:
        """Get the MC object from the network by its name

        Args:
            name: The string name of the MassComposition object stored on an edge in the network.

        Returns:

        """

        res: Optional[Union[Stream, MassComposition]] = None
        for u, v, a in self.graph.edges(data=True):
            if a['mc'].name == name:
                res = a['mc']

        if not res:
            raise ValueError(f"The specified name: {name} is not found on the network.")

        return res

    def get_stream_names(self) -> List[str]:
        """Get the names of the streams (MC objects on the edges)

        Returns:

        """

        res: List = []
        for u, v, a in self.graph.edges(data=True):
            res.append(a['mc'].name)
        return res

    def get_input_streams(self) -> List[Union[Stream, MassComposition]]:
        """Get the input (feed) streams (edge objects)

        Returns:
            List of MassComposition objects
        """

        # Create a dictionary that maps node names to their degrees
        degrees = {n: d for n, d in self.graph.degree()}

        res: List[Union[Stream, MassComposition]] = [d['mc'] for u, v, d in self.graph.edges(data=True) if
                                                     degrees[u] == 1]
        return res

    def get_output_streams(self) -> List[Union[Stream, MassComposition]]:
        """Get the output (product) streams (edge objects)

        Returns:
            List of MassComposition objects
        """

        # Create a dictionary that maps node names to their degrees
        degrees = {n: d for n, d in self.graph.degree()}

        res: List[Union[Stream, MassComposition]] = [d['mc'] for u, v, d in self.graph.edges(data=True) if
                                                     degrees[v] == 1]
        return res

    def get_column_formats(self, columns: List[str], strip_percent: bool = False) -> Dict[str, str]:
        """

        Args:
            columns: The columns to lookup format strings for
            strip_percent: If True remove the leading % symbol from the format (for plotly tables)

        Returns:

        """
        variables = self.get_input_streams()[0].variables
        d_format: Dict = {}
        for col in columns:
            for v in variables.vars.variables:
                if col in [v.column_name, v.name]:
                    d_format[col] = v.format
                    if strip_percent:
                        d_format[col] = d_format[col].strip('%')

        return d_format

    def report(self, apply_formats: bool = False) -> pd.DataFrame:
        """Summary Report

        Total Mass and weight averaged composition
        Returns:

        """
        chunks: List[pd.DataFrame] = []
        for n, nbrs in self.graph.adj.items():
            for nbr, eattr in nbrs.items():
                if eattr['mc'].data.to_dataframe().empty:
                    raise KeyError("Cannot generate report on empty dataset")
                chunks.append(eattr['mc'].aggregate().assign(name=eattr['mc'].name))
        rpt: pd.DataFrame = pd.concat(chunks, axis='index').set_index('name')
        if apply_formats:
            fmts: Dict = self.get_column_formats(rpt.columns)
            for k, v in fmts.items():
                rpt[k] = rpt[k].apply((v.replace('%', '{:,') + '}').format)
        return rpt

    def imbalance_report(self, node: int):
        mc_node: MCNode = self.graph.nodes[node]['mc']
        rpt: Path = mc_node.imbalance_report()
        webbrowser.open(str(rpt))

    def query(self, mc_name: str, queries: Dict) -> 'Flowsheet':
        """Query/filter across the network

        The queries provided will be applied to the MassComposition object in the network with the mc_name.
        The indexes for that result are then used to filter the other edges of the network.

        Args:
            mc_name: The name of the MassComposition object in the network to which the first filter to be applied.
            queries: The query or queries to apply to the object with mc_name.

        Returns:

        """

        mc_obj_ref: MassComposition = self.get_edge_by_name(mc_name).query(queries=queries)
        # TODO: This construct limits us to filtering along a single dimension only
        coord: str = list(queries.keys())[0]
        index = mc_obj_ref.data[coord]

        # iterate through all other objects on the edges and filter them to the same indexes
        mc_objects: List[Union[Stream, MassComposition]] = []
        for u, v, a in self.graph.edges(data=True):
            if a['mc'].name == mc_name:
                mc_objects.append(mc_obj_ref)
            else:
                mc_obj: MassComposition = deepcopy(self.get_edge_by_name(a['mc'].name))
                mc_obj._data = mc_obj._data.sel({coord: index.values})
                mc_objects.append(mc_obj)

        res: Flowsheet = Flowsheet.from_streams(mc_objects)

        return res

    def get_node_input_outputs(self, node) -> Tuple:
        in_edges = self.graph.in_edges(node)
        in_mc = [self.graph.get_edge_data(oe[0], oe[1])['mc'] for oe in in_edges]
        out_edges = self.graph.out_edges(node)
        out_mc = [self.graph.get_edge_data(oe[0], oe[1])['mc'] for oe in out_edges]
        return in_mc, out_mc

    def plot(self, orientation: str = 'horizontal') -> plt.Figure:
        """Plot the network with matplotlib

        Args:
            orientation: 'horizontal'|'vertical' network layout

        Returns:

        """

        hf, ax = plt.subplots()
        # pos = nx.spring_layout(self, seed=1234)
        pos = digraph_linear_layout(self.graph, orientation=orientation)

        edge_labels: Dict = {}
        edge_colors: List = []
        node_colors: List = []

        for node1, node2, data in self.graph.edges(data=True):
            edge_labels[(node1, node2)] = data['mc'].name
            if data['mc'].status.ok:
                edge_colors.append('gray')
            else:
                edge_colors.append('red')

        for n in self.graph.nodes:
            if self.graph.nodes[n]['mc'].node_type == NodeType.BALANCE:
                if self.graph.nodes[n]['mc'].balanced:
                    node_colors.append('green')
                else:
                    node_colors.append('red')
            else:
                node_colors.append('gray')

        nx.draw(self.graph, pos=pos, ax=ax, with_labels=True, font_weight='bold',
                node_color=node_colors, edge_color=edge_colors)

        nx.draw_networkx_edge_labels(self.graph, pos=pos, ax=ax, edge_labels=edge_labels, font_color='black')
        ax.set_title(self._plot_title(html=False), fontsize=10)

        return hf

    def plot_balance(self, facet_col_wrap: int = 3,
                     color: Optional[str] = 'node') -> go.Figure:
        """Plot input versus output across all nodes in the network

        Args:
            facet_col_wrap: the number of subplots per row before wrapping
            color: The optional variable to color by. If None color will be by Node

        Returns:

        """
        # prepare the data
        chunks_in: List = []
        chunks_out: List = []
        for n in self.graph.nodes:
            if self.graph.nodes[n]['mc'].node_type == NodeType.BALANCE:
                chunks_in.append(self.graph.nodes[n]['mc'].add('in').assign(**{'direction': 'in', 'node': n}))
                chunks_out.append(self.graph.nodes[n]['mc'].add('out').assign(**{'direction': 'out', 'node': n}))
        df_in: pd.DataFrame = pd.concat(chunks_in)
        index_names = ['direction', 'node'] + df_in.index.names
        df_in = df_in.reset_index().melt(id_vars=index_names)
        df_out: pd.DataFrame = pd.concat(chunks_out).reset_index().melt(id_vars=index_names)
        df_plot: pd.DataFrame = pd.concat([df_in, df_out])
        df_plot = df_plot.set_index(index_names + ['variable'], append=True).unstack(['direction'])
        df_plot.columns = df_plot.columns.droplevel(0)
        df_plot.reset_index(level=list(np.arange(-1, -len(index_names) - 1, -1)), inplace=True)
        df_plot['node'] = pd.Categorical(df_plot['node'])

        # plot
        fig = comparison_plot(data=df_plot,
                              x='in', y='out',
                              facet_col_wrap=facet_col_wrap,
                              color=color)
        return fig

    def plot_network(self, orientation: str = 'horizontal') -> go.Figure:
        """Plot the network with plotly

        Args:
            orientation: 'horizontal'|'vertical' network layout

        Returns:

        """
        # pos = nx.spring_layout(self, seed=1234)
        pos = digraph_linear_layout(self.graph, orientation=orientation)

        edge_traces, node_trace, edge_annotation_trace = self._get_scatter_node_edges(pos)
        title = self._plot_title()

        fig = go.Figure(data=[*edge_traces, node_trace, edge_annotation_trace],
                        layout=go.Layout(
                            title=title,
                            titlefont_size=16,
                            showlegend=False,
                            hovermode='closest',
                            margin=dict(b=20, l=5, r=5, t=40),
                            xaxis=dict(showgrid=False, zeroline=False, showticklabels=False),
                            yaxis=dict(showgrid=False, zeroline=False, showticklabels=False),
                            paper_bgcolor='rgba(0,0,0,0)',
                            plot_bgcolor='rgba(0,0,0,0)'
                        ),
                        )
        # for k, d_args in edge_annotations.items():
        #     fig.add_annotation(x=d_args['pos'][0], y=d_args['pos'][1], text=k, textangle=d_args['angle'])

        return fig

    def plot_sankey(self,
                    width_var: str = 'mass_wet',
                    color_var: Optional[str] = None,
                    edge_colormap: Optional[str] = 'copper_r',
                    vmin: Optional[float] = None,
                    vmax: Optional[float] = None,
                    ) -> go.Figure:
        """Plot the Network as a sankey

        Args:
            width_var: The variable that determines the sankey width
            color_var: The optional variable that determines the sankey edge color
            edge_colormap: The optional colormap.  Used with color_var.
            vmin: The value that maps to the minimum color
            vmax: The value that maps to the maximum color

        Returns:

        """
        # Create a mapping of node names to indices, and the integer nodes
        node_indices = {node: index for index, node in enumerate(self.graph.nodes)}
        int_graph = nx.relabel_nodes(self.graph, node_indices)

        # Generate the sankey diagram arguments using the new graph with integer nodes
        d_sankey = self._generate_sankey_args(int_graph, color_var, edge_colormap, width_var, vmin, vmax)

        # Create the sankey diagram
        node, link = self._get_sankey_node_link_dicts(d_sankey)
        fig = go.Figure(data=[go.Sankey(node=node, link=link)])
        title = self._plot_title()
        fig.update_layout(title_text=title, font_size=10)
        return fig

    def table_plot(self,
                   plot_type: str = 'sankey',
                   cols_exclude: Optional[List] = None,
                   table_pos: str = 'left',
                   table_area: float = 0.4,
                   table_header_color: str = 'cornflowerblue',
                   table_odd_color: str = 'whitesmoke',
                   table_even_color: str = 'lightgray',
                   sankey_width_var: str = 'mass_wet',
                   sankey_color_var: Optional[str] = None,
                   sankey_edge_colormap: Optional[str] = 'copper_r',
                   sankey_vmin: Optional[float] = None,
                   sankey_vmax: Optional[float] = None,
                   network_orientation: Optional[str] = 'horizontal'
                   ) -> go.Figure:
        """Plot with table of edge averages

        Args:
            plot_type: The type of plot ['sankey', 'network']
            cols_exclude: List of columns to exclude from the table
            table_pos: Position of the table ['left', 'right', 'top', 'bottom']
            table_area: The proportion of width or height to allocate to the table [0, 1]
            table_header_color: Color of the table header
            table_odd_color: Color of the odd table rows
            table_even_color: Color of the even table rows
            sankey_width_var: If plot_type is sankey, the variable that determines the sankey width
            sankey_color_var: If plot_type is sankey, the optional variable that determines the sankey edge color
            sankey_edge_colormap: If plot_type is sankey, the optional colormap.  Used with sankey_color_var.
            sankey_vmin: The value that maps to the minimum color
            sankey_vmax: The value that maps to the maximum color
            network_orientation: The orientation of the network layout 'vertical'|'horizontal'

        Returns:

        """

        valid_plot_types: List[str] = ['sankey', 'network']
        if plot_type not in valid_plot_types:
            raise ValueError(f'The supplied plot_type is not in {valid_plot_types}')

        valid_table_pos: List[str] = ['top', 'bottom', 'left', 'right']
        if table_pos not in valid_table_pos:
            raise ValueError(f'The supplied table_pos is not in {valid_table_pos}')

        d_subplot, d_table, d_plot = self._get_position_kwargs(table_pos, table_area, plot_type)

        fig = make_subplots(**d_subplot, print_grid=False)

        df: pd.DataFrame = self.report().reset_index()
        if cols_exclude:
            df = df[[col for col in df.columns if col not in cols_exclude]]
        fmt: List[str] = ['%s'] + list(self.get_column_formats(df.columns, strip_percent=True).values())
        column_widths = [2] + [1] * (len(df.columns) - 1)

        fig.add_table(
            header=dict(values=list(df.columns),
                        fill_color=table_header_color,
                        align='center',
                        font=dict(color='black', size=12)),
            columnwidth=column_widths,
            cells=dict(values=df.transpose().values.tolist(),
                       align='left', format=fmt,
                       fill_color=[
                           [table_odd_color if i % 2 == 0 else table_even_color for i in range(len(df))] * len(
                               df.columns)]),
            **d_table)

        if plot_type == 'sankey':
            # Create a mapping of node names to indices, and the integer nodes
            node_indices = {node: index for index, node in enumerate(self.graph.nodes)}
            int_graph = nx.relabel_nodes(self.graph, node_indices)

            # Generate the sankey diagram arguments using the new graph with integer nodes
            d_sankey = self._generate_sankey_args(int_graph, sankey_color_var,
                                                  sankey_edge_colormap,
                                                  sankey_width_var,
                                                  sankey_vmin,
                                                  sankey_vmax)
            node, link = self._get_sankey_node_link_dicts(d_sankey)
            fig.add_trace(go.Sankey(node=node, link=link), **d_plot)

        elif plot_type == 'network':
            # pos = nx.spring_layout(self, seed=1234)
            pos = digraph_linear_layout(self.graph, orientation=network_orientation)

            edge_traces, node_trace, edge_annotation_trace = self._get_scatter_node_edges(pos)
            fig.add_traces(data=[*edge_traces, node_trace, edge_annotation_trace], **d_plot)

            fig.update_layout(showlegend=False, hovermode='closest',
                              xaxis=dict(showgrid=False, zeroline=False, showticklabels=False),
                              yaxis=dict(showgrid=False, zeroline=False, showticklabels=False),
                              paper_bgcolor='rgba(0,0,0,0)',
                              plot_bgcolor='rgba(0,0,0,0)'
                              )

        title = self._plot_title(compact=True)
        fig.update_layout(title_text=title, font_size=12)

        return fig

    def to_dataframe(self,
                     names: Optional[str] = None):
        """Return a tidy dataframe

        Adds the mc name to the index so indexes are unique.

        Args:
            names: Optional List of names of MassComposition objects (network edges) for export

        Returns:

        """
        chunks: List[pd.DataFrame] = []
        for u, v, data in self.graph.edges(data=True):
            if (names is None) or ((names is not None) and (data['mc'].name in names)):
                chunks.append(data['mc'].data.mc.to_dataframe().assign(name=data['mc'].name))
        return pd.concat(chunks, axis='index').set_index('name', append=True)

    def plot_parallel(self,
                      names: Optional[str] = None,
                      color: Optional[str] = None,
                      vars_include: Optional[List[str]] = None,
                      vars_exclude: Optional[List[str]] = None,
                      title: Optional[str] = None,
                      include_dims: Optional[Union[bool, List[str]]] = True,
                      plot_interval_edges: bool = False) -> go.Figure:
        """Create an interactive parallel plot

        Useful to explore multidimensional data like mass-composition data

        Args:
            names: Optional List of Names to plot
            color: Optional color variable
            vars_include: Optional List of variables to include in the plot
            vars_exclude: Optional List of variables to exclude in the plot
            title: Optional plot title
            include_dims: Optional boolean or list of dimension to include in the plot.  True will show all dims.
            plot_interval_edges: If True, interval edges will be plotted instead of interval mid

        Returns:

        """
        df: pd.DataFrame = self.to_dataframe(names=names)

        if not title and hasattr(self, 'name'):
            title = self.name

        fig = parallel_plot(data=df, color=color, vars_include=vars_include, vars_exclude=vars_exclude, title=title,
                            include_dims=include_dims, plot_interval_edges=plot_interval_edges)
        return fig

    def set_stream_parent(self, stream: str, parent: str):
        mc: MassComposition = self.get_edge_by_name(stream)
        mc.set_parent_node(self.get_edge_by_name(parent))
        self._update_graph(mc)

    def set_stream_child(self, stream: str, child: str):
        mc: MassComposition = self.get_edge_by_name(stream)
        mc.set_child_node(self.get_edge_by_name(child))
        self._update_graph(mc)

    def set_stream_nodes(self, stream: str, nodes: Tuple[int, int]):
        mc: MassComposition = self.get_edge_by_name(stream)
        mc.set_stream_nodes(nodes=nodes)
        self._update_graph(mc)

    def reset_stream_nodes(self, stream: Optional[str] = None):

        """Reset stream nodes to break relationships

        Args:
            stream: The optional stream (edge) within the network.
              If None all streams nodes on the network will be reset.


        Returns:

        """
        if stream is None:
            streams: Dict[str, MassComposition] = self.streams_to_dict()
            for k, v in streams.items():
                streams[k] = v.set_stream_nodes((random_int(), random_int()))
            self.graph = Flowsheet(name=self.name).from_streams(streams=list(streams.values())).graph
        else:
            mc: MassComposition = self.get_edge_by_name(stream)
            mc.set_stream_nodes((random_int(), random_int()))
            self._update_graph(mc)

    def _update_graph(self, mc: MassComposition):
        """Update the graph with an existing stream object

        Args:
            mc: The stream object

        Returns:

        """
        # brutal approach - rebuild from streams
        strms: List[Union[Stream, MassComposition]] = []
        for u, v, a in self.graph.edges(data=True):
            if a['mc'].name == mc.name:
                strms.append(mc)
            else:
                strms.append(a['mc'])
        self.graph = Flowsheet(name=self.name).from_streams(streams=strms).graph

    def set_node_names(self, node_names: Dict[int, str]):
        """Set the names of network nodes with a Dict
        """
        for node in node_names.keys():
            if ('mc' in self.graph.nodes[node].keys()) and (node in node_names.keys()):
                self.graph.nodes[node]['mc'].node_name = node_names[node]

    def set_stream_data(self, stream_data: Dict[str, MassComposition]):
        """Set the data (MassComposition) of network edges (streams) with a Dict
        """
        for stream_name, stream_data in stream_data.items():
            for u, v, data in self.graph.edges(data=True):
                if ('mc' in data.keys()) and (data['mc'].name == stream_name):
                    self._logger.info(f'Setting data on stream {stream_name}')
                    data['mc'] = stream_data
                    # refresh the node status
                    for node in [u, v]:
                        self.graph.nodes[node]['mc'].inputs = [self.graph.get_edge_data(e[0], e[1])['mc'] for e in
                                                               self.graph.in_edges(node)]
                        self.graph.nodes[node]['mc'].outputs = [self.graph.get_edge_data(e[0], e[1])['mc'] for e in
                                                                self.graph.out_edges(node)]

    def streams_to_dict(self) -> Dict[str, MassComposition]:
        """Export the Stream objects to a Dict

        Returns:
            A dictionary keyed by name containing MassComposition objects

        """
        streams: Dict[str, MassComposition] = {}
        for u, v, data in self.graph.edges(data=True):
            if 'mc' in data.keys():
                streams[data['mc'].name] = data['mc']
        return streams

    def nodes_to_dict(self) -> Dict[int, MCNode]:
        """Export the MCNode objects to a Dict

        Returns:
            A dictionary keyed by integer containing MCNode objects

        """
        nodes: Dict[int, MCNode] = {}
        for node in self.graph.nodes.keys():
            if 'mc' in self.graph.nodes[node].keys():
                nodes[node] = self.graph.nodes[node]['mc']
        return nodes

    @staticmethod
    def _get_position_kwargs(table_pos, table_area, plot_type):
        """Helper to manage location dependencies

        Args:
            table_pos: position of the table: left|right|top|bottom
            table_width: fraction of the plot to assign to the table [0, 1]

        Returns:

        """
        name_type_map: Dict = {'sankey': 'sankey', 'network': 'xy'}
        specs = [[{"type": 'table'}, {"type": name_type_map[plot_type]}]]

        widths: Optional[List[float]] = [table_area, 1.0 - table_area]
        subplot_kwargs: Dict = {'rows': 1, 'cols': 2, 'specs': specs}
        table_kwargs: Dict = {'row': 1, 'col': 1}
        plot_kwargs: Dict = {'row': 1, 'col': 2}

        if table_pos == 'left':
            subplot_kwargs['column_widths'] = widths
        elif table_pos == 'right':
            subplot_kwargs['column_widths'] = widths[::-1]
            subplot_kwargs['specs'] = [[{"type": name_type_map[plot_type]}, {"type": 'table'}]]
            table_kwargs['col'] = 2
            plot_kwargs['col'] = 1
        else:
            subplot_kwargs['rows'] = 2
            subplot_kwargs['cols'] = 1
            table_kwargs['col'] = 1
            plot_kwargs['col'] = 1
            if table_pos == 'top':
                subplot_kwargs['row_heights'] = widths
                subplot_kwargs['specs'] = [[{"type": 'table'}], [{"type": name_type_map[plot_type]}]]
                table_kwargs['row'] = 1
                plot_kwargs['row'] = 2
            elif table_pos == 'bottom':
                subplot_kwargs['row_heights'] = widths[::-1]
                subplot_kwargs['specs'] = [[{"type": name_type_map[plot_type]}], [{"type": 'table'}]]
                table_kwargs['row'] = 2
                plot_kwargs['row'] = 1

        if plot_type == 'network':  # different arguments for different plots
            plot_kwargs = {f'{k}s': v for k, v in plot_kwargs.items()}

        return subplot_kwargs, table_kwargs, plot_kwargs

    def _generate_sankey_args(self, int_graph, color_var, edge_colormap, width_var, v_min, v_max):
        rpt: pd.DataFrame = self.report()
        if color_var is not None:
            cmap = sns.color_palette(edge_colormap, as_cmap=True)
            rpt: pd.DataFrame = self.report()
            if not v_min:
                v_min = np.floor(rpt[color_var].min())
            if not v_max:
                v_max = np.ceil(rpt[color_var].max())

        # run the report for the hover data
        d_custom_data: Dict = self._rpt_to_html(df=rpt)
        source: List = []
        target: List = []
        value: List = []
        edge_custom_data = []
        edge_color: List = []
        edge_labels: List = []
        node_colors: List = []
        node_labels: List = []

        for n in int_graph.nodes:
            if int_graph.nodes[n]['mc'].node_name != 'Node':
                node_labels.append(int_graph.nodes[n]['mc'].node_name)
            else:
                node_labels.append(str(n))  # the integer string

            if int_graph.nodes[n]['mc'].node_type == NodeType.BALANCE:
                if int_graph.nodes[n]['mc'].balanced:
                    node_colors.append('green')
                else:
                    node_colors.append('red')
            else:
                node_colors.append('blue')

        for u, v, data in int_graph.edges(data=True):
            edge_labels.append(data['mc'].name)
            source.append(u)
            target.append(v)
            value.append(float(data['mc'].aggregate()[width_var].iloc[0]))
            edge_custom_data.append(d_custom_data[data['mc'].name])

            if color_var is not None:
                val: float = float(data['mc'].aggregate()[color_var].iloc[0])
                str_color: str = f'rgba{self._color_from_float(v_min, v_max, val, cmap)}'
                edge_color.append(str_color)
            else:
                edge_color: Optional[str] = None

        d_sankey: Dict = {'node_color': node_colors,
                          'edge_color': edge_color,
                          'edge_custom_data': edge_custom_data,
                          'edge_labels': edge_labels,
                          'labels': node_labels,
                          'source': source,
                          'target': target,
                          'value': value}

        return d_sankey

    @staticmethod
    def _get_sankey_node_link_dicts(d_sankey: Dict):
        node: Dict = dict(
            pad=15,
            thickness=20,
            line=dict(color="black", width=0.5),
            label=d_sankey['labels'],
            color=d_sankey['node_color'],
            customdata=d_sankey['labels']
        )
        link: Dict = dict(
            source=d_sankey['source'],  # indices correspond to labels, eg A1, A2, A1, B1, ...
            target=d_sankey['target'],
            value=d_sankey['value'],
            color=d_sankey['edge_color'],
            label=d_sankey['edge_labels'],  # over-written by hover template
            customdata=d_sankey['edge_custom_data'],
            hovertemplate='<b><i>%{label}</i></b><br />Source: %{source.customdata}<br />'
                          'Target: %{target.customdata}<br />%{customdata}'
        )
        return node, link

    def _get_scatter_node_edges(self, pos):
        # edges
        edge_color_map: Dict = {True: 'grey', False: 'red'}
        edge_annotations: Dict = {}

        edge_traces = []
        for u, v, data in self.graph.edges(data=True):
            x0, y0 = pos[u]
            x1, y1 = pos[v]
            edge_annotations[data['mc'].name] = {'pos': midpoint(pos[u], pos[v])}
            edge_traces.append(go.Scatter(x=[x0, x1], y=[y0, y1],
                                          line=dict(width=2, color=edge_color_map[data['mc'].status.ok]),
                                          hoverinfo='text',
                                          mode='lines+markers',
                                          text=data['mc'].name,
                                          marker=dict(
                                              symbol="arrow",
                                              color=edge_color_map[data['mc'].status.ok],
                                              size=16,
                                              angleref="previous",
                                              standoff=15)
                                          ))

        # nodes
        node_color_map: Dict = {None: 'grey', True: 'green', False: 'red'}
        node_x = []
        node_y = []
        node_color = []
        node_text = []
        for node in self.graph.nodes():
            x, y = pos[node]
            node_x.append(x)
            node_y.append(y)
            node_color.append(node_color_map[self.graph.nodes[node]['mc'].balanced])
            node_text.append(node)
        node_trace = go.Scatter(
            x=node_x, y=node_y,
            mode='markers+text',
            hoverinfo='none',
            marker=dict(
                color=node_color,
                size=30,
                line_width=2),
            text=node_text)

        # edge annotations
        edge_labels = list(edge_annotations.keys())
        edge_label_x = [edge_annotations[k]['pos'][0] for k, v in edge_annotations.items()]
        edge_label_y = [edge_annotations[k]['pos'][1] for k, v in edge_annotations.items()]

        edge_annotation_trace = go.Scatter(
            x=edge_label_x, y=edge_label_y,
            mode='markers',
            hoverinfo='text',
            marker=dict(
                color='grey',
                size=3,
                line_width=1),
            text=edge_labels)

        return edge_traces, node_trace, edge_annotation_trace

    def _rpt_to_html(self, df: pd.DataFrame) -> Dict:
        custom_data: Dict = {}
        fmts: Dict = self.get_column_formats(df.columns)
        for i, row in df.iterrows():
            str_data: str = '<br />'
            for k, v in dict(row).items():
                str_data += f"{k}: {v:{fmts[k][1:]}}<br />"
            custom_data[i] = str_data
        return custom_data

    @staticmethod
    def _color_from_float(vmin: float, vmax: float, val: float,
                          cmap: Union[ListedColormap, LinearSegmentedColormap]) -> Tuple[float, float, float]:
        if isinstance(cmap, ListedColormap):
            color_index: int = int((val - vmin) / ((vmax - vmin) / 256.0))
            color_index = min(max(0, color_index), 255)
            color_rgba = tuple(cmap.colors[color_index])
        elif isinstance(cmap, LinearSegmentedColormap):
            norm = matplotlib.colors.Normalize(vmin=vmin, vmax=vmax)
            m = cm.ScalarMappable(norm=norm, cmap=cmap)
            r, g, b, a = m.to_rgba(val, bytes=True)
            color_rgba = int(r), int(g), int(b), int(a)
        else:
            NotImplementedError("Unrecognised colormap type")

        return color_rgba

    def _plot_title(self, html: bool = True, compact: bool = False):
        title = f"{self.name}<br><br><sup>Balanced: {self.balanced}<br>Edge Status OK: {self.edge_status[0]}</sup>"
        if compact:
            title = title.replace("<br><br>", "<br>").replace("<br>Edge", ", Edge")
        if not self.edge_status[0]:
            title = title.replace("</sup>", "") + f", {self.edge_status[1]}</sup>"
        if not html:
            title = title.replace('<br><br>', '\n').replace('<br>', '\n').replace('<sup>', '').replace('</sup>', '')
        return title

    @classmethod
    def _check_indexes(cls, streams):
        logger: logging.Logger = logging.getLogger(__class__.__name__)

        list_of_indexes = [s.data.to_dataframe().index for s in streams]
        types_of_indexes = [type(i) for i in list_of_indexes]
        # check the index types are consistent
        if len(set(types_of_indexes)) != 1:
            raise KeyError("stream index types are not consistent")

        # check the shapes are consistent
        if len(np.unique([i.shape for i in list_of_indexes])) != 1:
            if list_of_indexes[0].names == ['size']:
                logger.debug(f"size index detected - attempting index alignment")
                # two failure modes can be managed:
                # 1) missing coarse size fractions - can be added with zeros
                # 2) missing intermediate fractions - require interpolation to preserve mass
                df_streams: pd.DataFrame = pd.concat([s.data.to_dataframe().assign(stream=s.name) for s in streams])
                df_streams_full = df_streams.pivot(columns=['stream'])
                df_streams_full.columns.names = ['component', 'stream']
                df_streams_full.sort_index(ascending=False, inplace=True)
                stream_nans: pd.DataFrame = df_streams_full.isna().stack(level=-1)

                for stream in streams:
                    s: str = stream.name
                    tmp_nans: pd.Series = stream_nans.query('stream==@s').sum(axis=1)
                    if tmp_nans.iloc[0] > 0:
                        logger.debug(f'The {s} stream has missing coarse sizes')
                        first_zero_index = tmp_nans.loc[tmp_nans == 0].index[0]
                        if tmp_nans[tmp_nans.index <= first_zero_index].sum() > 0:
                            logger.debug(f'The {s} stream has missing sizes requiring interpolation')
                            raise NotImplementedError('Coming soon - we need interpolation!')
                        else:
                            logger.debug(f'The {s} stream has missing coarse sizes only')
                            stream_df = df_streams_full.loc[:, (slice(None), s)].droplevel(-1, axis=1).fillna(0)
                            # recreate the stream from the dataframe
                            stream.set_data(stream_df)
            else:
                raise KeyError("stream index shapes are not consistent")
        return streams
