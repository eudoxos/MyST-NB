from docutils import nodes
import nbformat as nbf

from myst_parser.docutils_renderer import SphinxRenderer, dict_to_docinfo
from myst_parser.block_tokens import tokenize
from myst_parser.sphinx_parser import MystParser
from jupyter_sphinx.execute import get_widgets, contains_widgets, JupyterWidgetStateNode


class NotebookParser(MystParser):
    """Docutils parser for IPynb + CommonMark + Math + Tables + RST Extensions """

    supported = ("ipynb",)
    translate_section_name = None

    default_config = {"known_url_schemes": None}

    config_section = "ipynb parser"
    config_section_dependencies = ("parsers",)

    def parse(self, inputstring, document):
        self.config = self.default_config.copy()
        try:
            new_cfg = document.settings.env.config.myst_config
            self.config.update(new_cfg)
        except AttributeError:
            pass

        ntbk = nbf.reads(inputstring, nbf.NO_CONVERT)

        # Parse notebook-level metadata as front-matter
        # For now, only keep key/val pairs that point to int/float/string
        metadata = ntbk.metadata
        docinfo = dict_to_docinfo(metadata)
        document += docinfo

        # If there are widgets, this will embed the state of all widgets in a script
        if contains_widgets(ntbk):
            document.append(JupyterWidgetStateNode(state=get_widgets(ntbk)))
        renderer = SphinxRenderer(document=document, current_node=None)
        with renderer:
            # Loop through cells and render them
            for cell in ntbk.cells:
                # Skip empty cells
                if len(cell["source"]) == 0:
                    continue

                # Cell container will wrap whatever is in the cell
                classes = ["cell"]
                for tag in cell.metadata.get("tags", []):
                    classes.append(f"tag_{tag}")

                # If a markdown cell, simply call the Myst parser and append children
                if cell["cell_type"] == "markdown":
                    source = cell["source"]
                    if not source.endswith("\n"):
                        # Ensure we always end with a newline in case it's a one-liner
                        source = source + "\n"
                    myst_ast = tokenize(source.splitlines(keepends=True))

                    # Check for tag-specific behavior
                    if "hide_input" in cell.metadata.get("tags", []):
                        container = nodes.container()
                        container["classes"].extend(["toggle"])
                        with renderer.current_node_context(container, append=True):
                            for child in myst_ast:
                                renderer.render(child)
                    else:
                        for child in myst_ast:
                            renderer.render(child)

                # If a code cell, convert the code + outputs
                elif cell["cell_type"] == "code":
                    sphinx_cell = CellNode(classes=classes, cell_type=cell["cell_type"])
                    cell_input = CellInputNode(classes=["cell_input"])
                    sphinx_cell += cell_input
                    renderer.current_node += sphinx_cell
                    # Input block
                    code_block = nodes.literal_block(text=cell["source"])
                    cell_input += code_block

                    # ==================
                    # Cell output
                    # ==================
                    cell_output = CellOutputNode(classes=["cell_output"])
                    sphinx_cell += cell_output

                    outputs = CellOutputBundleNode(cell["outputs"])
                    cell_output += outputs


class CellNode(nodes.container):
    """Represent a cell in the Sphinx AST."""

    def __init__(self, rawsource="", *children, **attributes):
        super().__init__("", **attributes)


class CellInputNode(nodes.container):
    """Represent an input cell in the Sphinx AST."""

    def __init__(self, rawsource="", *children, **attributes):
        super().__init__("", **attributes)


class CellOutputNode(nodes.container):
    """Represent an output cell in the Sphinx AST."""

    def __init__(self, rawsource="", *children, **attributes):
        super().__init__("", **attributes)


class CellOutputBundleNode(nodes.container):
    """Represent a MimeBundle in the Sphinx AST, to be transformed later."""

    def __init__(self, outputs, rawsource="", *children, **attributes):
        self.outputs = outputs
        super().__init__("", **attributes)


class CellImageNode(nodes.image):
    """An inline image that will output to an inline HTML image."""

    def __init__(self, rawsource="", *children, **attributes):
        super().__init__("", **attributes)
