"""A parser for docutils."""
from __future__ import annotations

from contextlib import suppress
from functools import partial
from importlib import resources as import_resources
import os
from typing import Any

from docutils import nodes
from docutils.core import default_description, publish_cmdline
from docutils.parsers.rst.directives import _directives
from docutils.parsers.rst.roles import _roles
from markdown_it.token import Token
from markdown_it.tree import SyntaxTreeNode
from myst_parser.docutils_ import DOCUTILS_EXCLUDED_ARGS as DOCUTILS_EXCLUDED_ARGS_MYST
from myst_parser.docutils_ import Parser as MystParser
from myst_parser.docutils_ import create_myst_config, create_myst_settings_spec
from myst_parser.docutils_renderer import DocutilsRenderer, token_line
from myst_parser.main import MdParserConfig, create_md_parser
import nbformat
from nbformat import NotebookNode
from pygments.formatters import get_formatter_by_name

from myst_nb import static
from myst_nb.core.config import NbParserConfig
from myst_nb.core.execute import execute_notebook
from myst_nb.core.loggers import DEFAULT_LOG_TYPE, DocutilsDocLogger
from myst_nb.core.parse import nb_node_to_dict, notebook_to_tokens
from myst_nb.core.preprocess import preprocess_notebook
from myst_nb.core.read import (
    NbReader,
    UnexpectedCellDirective,
    read_myst_markdown_notebook,
    standard_nb_read,
)
from myst_nb.core.render import (
    MimeData,
    NbElementRenderer,
    create_figure_context,
    get_mime_priority,
    load_renderer,
)
from myst_nb.glue import get_glue_directives, get_glue_roles

DOCUTILS_EXCLUDED_ARGS = list(
    {f.name for f in NbParserConfig.get_fields() if f.metadata.get("docutils_exclude")}
)


class Parser(MystParser):
    """Docutils parser for Jupyter Notebooks, containing MyST Markdown."""

    supported: tuple[str, ...] = ("mystnb", "ipynb")
    """Aliases this parser supports."""

    settings_spec = (
        "MyST-NB options",
        None,
        create_myst_settings_spec(DOCUTILS_EXCLUDED_ARGS, NbParserConfig, "nb_"),
        *MystParser.settings_spec,
    )
    """Runtime settings specification."""

    config_section = "myst-nb parser"

    def parse(self, inputstring: str, document: nodes.document) -> None:
        # register/unregister special directives and roles
        new_directives = get_glue_directives()
        new_directives["code-cell"] = UnexpectedCellDirective
        new_directives["raw-cell"] = UnexpectedCellDirective
        new_roles = get_glue_roles()
        for name, directive in new_directives.items():
            _directives[name] = directive
        for name, role in new_roles.items():
            _roles[name] = role
        try:
            return self._parse(inputstring, document)
        finally:
            for name in new_directives:
                _directives.pop(name, None)
            for name in new_roles:
                _roles.pop(name, None)

    def _parse(self, inputstring: str, document: nodes.document) -> None:
        """Parse source text.

        :param inputstring: The source string to parse
        :param document: The root docutils node to add AST elements to
        """
        document_source = document["source"]

        # get a logger for this document
        logger = DocutilsDocLogger(document)

        # get markdown parsing configuration
        try:
            md_config = create_myst_config(
                document.settings, DOCUTILS_EXCLUDED_ARGS_MYST
            )
        except (TypeError, ValueError) as error:
            logger.error(f"myst configuration invalid: {error.args[0]}")
            md_config = MdParserConfig()

        # get notebook rendering configuration
        try:
            nb_config = create_myst_config(
                document.settings, DOCUTILS_EXCLUDED_ARGS, NbParserConfig, "nb_"
            )
        except (TypeError, ValueError) as error:
            logger.error(f"myst-nb configuration invalid: {error.args[0]}")
            nb_config = NbParserConfig()

        # convert inputstring to notebook
        # note docutils does not support the full custom format mechanism
        if nb_config.read_as_md:
            nb_reader = NbReader(
                partial(
                    read_myst_markdown_notebook,
                    config=md_config,
                    add_source_map=True,
                ),
                md_config,
                {"type": "plugin", "name": "myst_nb_md"},
            )
        else:
            nb_reader = NbReader(standard_nb_read, md_config)
        notebook = nb_reader.read(inputstring)

        # Update mystnb configuration with notebook level metadata
        if nb_config.metadata_key in notebook.metadata:
            overrides = nb_node_to_dict(notebook.metadata[nb_config.metadata_key])
            try:
                nb_config = nb_config.copy(**overrides)
            except Exception as exc:
                logger.warning(
                    f"Failed to update configuration with notebook metadata: {exc}",
                    subtype="config",
                )
            else:
                logger.debug(
                    "Updated configuration with notebook metadata", subtype="config"
                )

        # potentially execute notebook and/or populate outputs from cache
        notebook, exec_data = execute_notebook(
            notebook, document_source, nb_config, logger
        )
        if exec_data:
            document["nb_exec_data"] = exec_data

        # Setup the markdown parser
        mdit_parser = create_md_parser(nb_reader.md_config, DocutilsNbRenderer)
        mdit_parser.options["document"] = document
        mdit_parser.options["notebook"] = notebook
        mdit_parser.options["nb_config"] = nb_config
        mdit_renderer: DocutilsNbRenderer = mdit_parser.renderer  # type: ignore
        mdit_env: dict[str, Any] = {}

        # load notebook element renderer class from entry-point name
        # this is separate from DocutilsNbRenderer, so that users can override it
        renderer_name = nb_config.render_plugin
        nb_renderer: NbElementRenderer = load_renderer(renderer_name)(
            mdit_renderer, logger
        )
        # we temporarily store nb_renderer on the document,
        # so that roles/directives can access it
        document.attributes["nb_renderer"] = nb_renderer
        # we currently do this early, so that the nb_renderer has access to things
        mdit_renderer.setup_render(mdit_parser.options, mdit_env)

        # pre-process notebook and store resources for render
        resources = preprocess_notebook(
            notebook, logger, mdit_renderer.get_cell_render_config
        )
        mdit_renderer.md_options["nb_resources"] = resources

        # parse to tokens
        mdit_tokens = notebook_to_tokens(notebook, mdit_parser, mdit_env, logger)
        # convert to docutils AST, which is added to the document
        mdit_renderer.render(mdit_tokens, mdit_parser.options, mdit_env)

        if nb_config.output_folder:
            # write final (updated) notebook to output folder (utf8 is standard encoding)
            content = nbformat.writes(notebook).encode("utf-8")
            nb_renderer.write_file(["processed.ipynb"], content, overwrite=True)

            # if we are using an HTML writer, dynamically add the CSS to the output
            if nb_config.append_css and hasattr(document.settings, "stylesheet"):
                css_paths = []

                css_paths.append(
                    nb_renderer.write_file(
                        ["mystnb.css"],
                        import_resources.read_binary(static, "mystnb.css"),
                        overwrite=True,
                    )
                )
                fmt = get_formatter_by_name("html", style="default")
                css_paths.append(
                    nb_renderer.write_file(
                        ["pygments.css"],
                        fmt.get_style_defs(".code").encode("utf-8"),
                        overwrite=True,
                    )
                )
                css_paths = [os.path.abspath(path) for path in css_paths]
                # stylesheet and stylesheet_path are mutually exclusive
                if document.settings.stylesheet_path:
                    document.settings.stylesheet_path.extend(css_paths)
                if document.settings.stylesheet:
                    document.settings.stylesheet.extend(css_paths)

            # TODO also handle JavaScript

        # remove temporary state
        document.attributes.pop("nb_renderer")


class DocutilsNbRenderer(DocutilsRenderer):
    """A docutils-only renderer for Jupyter Notebooks."""

    @property
    def nb_config(self) -> NbParserConfig:
        """Get the notebook element renderer."""
        return self.md_options["nb_config"]

    @property
    def nb_renderer(self) -> NbElementRenderer:
        """Get the notebook element renderer."""
        return self.document["nb_renderer"]

    def get_cell_render_config(
        self,
        cell_metadata: dict[str, Any],
        key: str,
        nb_key: str | None = None,
        has_nb_key: bool = True,
    ) -> Any:
        """Get a cell level render configuration value.

        :param has_nb_key: Whether to also look in the notebook level configuration
        :param nb_key: The notebook level configuration key to use if the cell
            level key is not found. if None, use the ``key`` argument

        :raises: IndexError if the cell index is out of range
        :raises: KeyError if the key is not found
        """
        # TODO allow output level configuration?
        use_nb_level = True
        cell_metadata_key = self.nb_config.cell_render_key
        if cell_metadata_key in cell_metadata:
            if isinstance(cell_metadata[cell_metadata_key], dict):
                if key in cell_metadata[cell_metadata_key]:
                    use_nb_level = False
            else:
                # TODO log warning
                pass
        if use_nb_level:
            if not has_nb_key:
                raise KeyError(key)
            return self.nb_config[nb_key if nb_key is not None else key]
        # TODO validate?
        return cell_metadata[cell_metadata_key][key]

    def render_nb_metadata(self, token: SyntaxTreeNode) -> None:
        """Render the notebook metadata."""
        metadata = dict(token.meta)
        special_keys = ("kernelspec", "language_info", "source_map")
        for key in special_keys:
            # save these special keys on the document, rather than as docinfo
            if key in metadata:
                self.document[f"nb_{key}"] = metadata.get(key)

        metadata = self.nb_renderer.render_nb_metadata(dict(token.meta))

        if self.nb_config.metadata_to_fm:
            # forward the remaining metadata to the front_matter renderer
            top_matter = {k: v for k, v in metadata.items() if k not in special_keys}
            self.render_front_matter(
                Token(  # type: ignore
                    "front_matter",
                    "",
                    0,
                    map=[0, 0],
                    content=top_matter,  # type: ignore[arg-type]
                ),
            )

    def render_nb_cell_markdown(self, token: SyntaxTreeNode) -> None:
        """Render a notebook markdown cell."""
        # TODO this is currently just a "pass-through", but we could utilise the metadata
        # it would be nice to "wrap" this in a container that included the metadata,
        # but unfortunately this would break the heading structure of docutils/sphinx.
        # perhaps we add an "invisible" (non-rendered) marker node to the document tree,
        self.render_children(token)

    def render_nb_cell_raw(self, token: SyntaxTreeNode) -> None:
        """Render a notebook raw cell."""
        line = token_line(token, 0)
        _nodes = self.nb_renderer.render_raw_cell(
            token.content, token.meta["metadata"], token.meta["index"], line
        )
        self.add_line_and_source_path_r(_nodes, token)
        self.current_node.extend(_nodes)

    def render_nb_cell_code(self, token: SyntaxTreeNode) -> None:
        """Render a notebook code cell."""
        cell_index = token.meta["index"]
        tags = token.meta["metadata"].get("tags", [])

        # TODO do we need this -/_ duplication of tag names, or can we deprecate one?
        remove_input = (
            self.get_cell_render_config(token.meta["metadata"], "remove_code_source")
            or ("remove_input" in tags)
            or ("remove-input" in tags)
        )
        remove_output = (
            self.get_cell_render_config(token.meta["metadata"], "remove_code_outputs")
            or ("remove_output" in tags)
            or ("remove-output" in tags)
        )

        # if we are remove both the input and output, we can skip the cell
        if remove_input and remove_output:
            return

        # create a container for all the input/output
        classes = ["cell"]
        for tag in tags:
            classes.append(f"tag_{tag.replace(' ', '_')}")
        cell_container = nodes.container(
            nb_element="cell_code",
            cell_index=cell_index,
            # TODO some way to use this to allow repr of count in outputs like HTML?
            exec_count=token.meta["execution_count"],
            cell_metadata=token.meta["metadata"],
            classes=classes,
        )
        self.add_line_and_source_path(cell_container, token)
        with self.current_node_context(cell_container, append=True):

            # render the code source code
            if not remove_input:
                cell_input = nodes.container(
                    nb_element="cell_code_source", classes=["cell_input"]
                )
                self.add_line_and_source_path(cell_input, token)
                with self.current_node_context(cell_input, append=True):
                    self.render_nb_cell_code_source(token)

            # render the execution output, if any
            has_outputs = self.md_options["notebook"]["cells"][cell_index].get(
                "outputs", []
            )
            if (not remove_output) and has_outputs:
                cell_output = nodes.container(
                    nb_element="cell_code_output", classes=["cell_output"]
                )
                self.add_line_and_source_path(cell_output, token)
                with self.current_node_context(cell_output, append=True):
                    self.render_nb_cell_code_outputs(token)

    def render_nb_cell_code_source(self, token: SyntaxTreeNode) -> None:
        """Render a notebook code cell's source."""
        lexer = token.meta.get("lexer", None)
        node = self.create_highlighted_code_block(
            token.content,
            lexer,
            number_lines=self.get_cell_render_config(
                token.meta["metadata"], "number_source_lines"
            ),
            source=self.document["source"],
            line=token_line(token),
        )
        self.add_line_and_source_path(node, token)
        self.current_node.append(node)

    def render_nb_cell_code_outputs(self, token: SyntaxTreeNode) -> None:
        """Render a notebook code cell's outputs."""
        cell_index = token.meta["index"]
        metadata = token.meta["metadata"]
        line = token_line(token)
        outputs: list[NotebookNode] = self.md_options["notebook"]["cells"][
            cell_index
        ].get("outputs", [])
        # render the outputs
        mime_priority = get_mime_priority(
            self.nb_config.builder_name, self.nb_config.mime_priority_overrides
        )
        for output_index, output in enumerate(outputs):
            if output.output_type == "stream":
                if output.name == "stdout":
                    _nodes = self.nb_renderer.render_stdout(
                        output, metadata, cell_index, line
                    )
                    self.add_line_and_source_path_r(_nodes, token)
                    self.current_node.extend(_nodes)
                elif output.name == "stderr":
                    _nodes = self.nb_renderer.render_stderr(
                        output, metadata, cell_index, line
                    )
                    self.add_line_and_source_path_r(_nodes, token)
                    self.current_node.extend(_nodes)
                else:
                    pass  # TODO warning
            elif output.output_type == "error":
                _nodes = self.nb_renderer.render_error(
                    output, metadata, cell_index, line
                )
                self.add_line_and_source_path_r(_nodes, token)
                self.current_node.extend(_nodes)
            elif output.output_type in ("display_data", "execute_result"):

                # Note, this is different to the sphinx implementation,
                # here we directly select a single output, based on the mime_priority,
                # as opposed to output all mime types, and select in a post-transform
                # (the mime_priority must then be set for the output format)

                # TODO how to output MyST Markdown?
                # currently text/markdown is set to be rendered as CommonMark only,
                # with headings dissallowed,
                # to avoid "side effects" if the mime is discarded but contained
                # targets, etc, and because we can't parse headings within containers.
                # perhaps we could have a config option to allow this?
                # - for non-commonmark, the text/markdown would always be considered
                #   the top priority, and all other mime types would be ignored.
                # - for headings, we would also need to parsing the markdown
                #   at the "top-level", i.e. not nested in container(s)

                try:
                    mime_type = next(x for x in mime_priority if x in output["data"])
                except StopIteration:
                    self.create_warning(
                        "No output mime type found from render_priority",
                        line=line,
                        append_to=self.current_node,
                        wtype=DEFAULT_LOG_TYPE,
                        subtype="mime_type",
                    )
                else:
                    figure_options = None
                    with suppress(KeyError):
                        figure_options = self.get_cell_render_config(
                            metadata, "figure", has_nb_key=False
                        )

                    with create_figure_context(self, figure_options, line):
                        _nodes = self.nb_renderer.render_mime_type(
                            MimeData(
                                mime_type,
                                output["data"][mime_type],
                                cell_metadata=metadata,
                                output_metadata=output.get("metadata", {}),
                                cell_index=cell_index,
                                output_index=output_index,
                                line=line,
                            ),
                        )
                        self.current_node.extend(_nodes)
                        self.add_line_and_source_path_r(_nodes, token)
            else:
                self.create_warning(
                    f"Unsupported output type: {output.output_type}",
                    line=line,
                    append_to=self.current_node,
                    wtype=DEFAULT_LOG_TYPE,
                    subtype="output_type",
                )


def _run_cli(
    writer_name: str, builder_name: str, writer_description: str, argv: list[str] | None
):
    """Run the command line interface for a particular writer."""
    publish_cmdline(
        parser=Parser(),
        writer_name=writer_name,
        description=(
            f"Generates {writer_description} from standalone MyST Notebook sources.\n"
            f"{default_description}\n"
            "External outputs are written to `--nb-output-folder`.\n"
        ),
        # to see notebook execution info by default
        settings_overrides={"report_level": 1, "nb_builder_name": builder_name},
        argv=argv,
    )


def cli_html(argv: list[str] | None = None) -> None:
    """Cmdline entrypoint for converting MyST to HTML."""
    _run_cli("html", "html", "(X)HTML documents", argv)


def cli_html5(argv: list[str] | None = None):
    """Cmdline entrypoint for converting MyST to HTML5."""
    _run_cli("html5", "html", "HTML5 documents", argv)


def cli_latex(argv: list[str] | None = None):
    """Cmdline entrypoint for converting MyST to LaTeX."""
    _run_cli("latex", "latex", "LaTeX documents", argv)


def cli_xml(argv: list[str] | None = None):
    """Cmdline entrypoint for converting MyST to XML."""
    _run_cli("xml", "xml", "Docutils-native XML", argv)


def cli_pseudoxml(argv: list[str] | None = None):
    """Cmdline entrypoint for converting MyST to pseudo-XML."""
    _run_cli("pseudoxml", "html", "pseudo-XML", argv)