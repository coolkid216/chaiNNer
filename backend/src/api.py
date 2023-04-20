from __future__ import annotations

import importlib
import os
import platform
import sys
from dataclasses import dataclass, field
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Literal,
    Tuple,
    TypedDict,
    TypeVar,
    Union,
)

from sanic.log import logger

from base_types import InputId, OutputId
from nodes.group import Group, GroupId, NestedGroup, NestedIdGroup
from nodes.properties.inputs.base_input import BaseInput
from nodes.properties.outputs.base_output import BaseOutput

KB = 1024**1
MB = 1024**2
GB = 1024**3


def _process_inputs(base_inputs: Iterable[Union[BaseInput, NestedGroup]]):
    inputs: List[BaseInput] = []
    groups: List[NestedIdGroup] = []

    def add_inputs(
        current: Iterable[Union[BaseInput, NestedGroup]]
    ) -> List[Union[InputId, NestedIdGroup]]:
        layout: List[Union[InputId, NestedIdGroup]] = []

        for x in current:
            if isinstance(x, Group):
                if x.info.id == -1:
                    x.info.id = GroupId(len(groups))
                g: NestedIdGroup = Group(x.info, [])
                groups.append(g)
                layout.append(g)
                g.items.extend(add_inputs(x.items))  # type: ignore
            else:
                if x.id == -1:
                    x.id = InputId(len(inputs))
                layout.append(x.id)
                inputs.append(x)

        return layout

    return inputs, add_inputs(base_inputs)


def _process_outputs(base_outputs: Iterable[BaseOutput]):
    outputs: List[BaseOutput] = []
    for i, output_value in enumerate(base_outputs):
        if output_value.id == -1:
            output_value.id = OutputId(i)
        outputs.append(output_value)
    return outputs


RunFn = Callable[..., Any]

NodeType = Literal["regularNode", "iterator", "iteratorHelper"]


class DefaultNode(TypedDict):
    schemaId: str


@dataclass(frozen=True)
class NodeData:
    schema_id: str
    description: str
    name: str
    icon: str
    type: NodeType

    inputs: List[BaseInput]
    outputs: List[BaseOutput]
    group_layout: List[Union[InputId, NestedIdGroup]]

    side_effects: bool
    deprecated: bool
    default_nodes: List[DefaultNode] | None  # For iterators only

    run: RunFn


T = TypeVar("T", bound=RunFn)


@dataclass
class NodeGroup:
    category: Category
    name: str
    nodes: List[NodeData] = field(default_factory=list)

    def add_node(self, node: NodeData):
        logger.debug(f"Added {node.schema_id}")
        self.nodes.append(node)

    def register(
        self,
        schema_id: str,
        name: str,
        description: str,
        inputs: List[Union[BaseInput, NestedGroup]],
        outputs: List[BaseOutput],
        icon: str = "BsQuestionCircleFill",
        node_type: NodeType = "regularNode",
        side_effects: bool = False,
        deprecated: bool = False,
        default_nodes: List[DefaultNode] | None = None,
    ):
        def inner_wrapper(wrapped_func: T) -> T:
            p_inputs, group_layout = _process_inputs(inputs)
            p_output = _process_outputs(outputs)

            node = NodeData(
                schema_id=schema_id,
                name=name,
                description=description,
                icon=icon,
                type=node_type,
                inputs=p_inputs,
                group_layout=group_layout,
                outputs=p_output,
                side_effects=side_effects,
                deprecated=deprecated,
                default_nodes=default_nodes,
                run=wrapped_func,
            )

            self.add_node(node)
            return wrapped_func

        return inner_wrapper


@dataclass
class Category:
    package: Package
    name: str
    description: str
    icon: str = "BsQuestionCircleFill"
    color: str = "#777777"
    install_hint: str | None = None
    node_groups: List["NodeGroup"] = field(default_factory=list)

    def add_node_group(self, name: str) -> "NodeGroup":
        result = NodeGroup(category=self, name=name)
        self.node_groups.append(result)
        return result

    def toDict(self):
        return {
            "name": self.name,
            "description": self.description,
            "icon": self.icon,
            "color": self.color,
            "installHint": self.install_hint,
        }


@dataclass
class Dependency:
    display_name: str
    package_name: str
    version: str
    size_estimate: int | float
    auto_update: bool = False
    extra_index_url: str | None = None

    import_name: str | None = None

    def toDict(self):
        return {
            "displayName": self.display_name,
            "packageName": self.package_name,
            "version": self.version,
            "sizeEstimate": int(self.size_estimate),
            "autoUpdate": self.auto_update,
            "findLink": self.extra_index_url,
        }


@dataclass
class Package:
    where: str
    name: str
    description: str
    dependencies: List[Dependency] = field(default_factory=list)
    categories: List[Category] = field(default_factory=list)

    def add_category(
        self,
        name: str,
        description: str,
        icon: str,
        color: str,
        install_hint: str | None = None,
    ) -> "Category":
        result = Category(
            package=self,
            name=name,
            description=description,
            icon=icon,
            color=color,
            install_hint=install_hint,
        )
        self.categories.append(result)
        return result

    def add_dependency(
        self,
        dependency: Dependency,
    ):
        self.dependencies.append(dependency)


def _iter_py_files(directory: str):
    for root, _, files in os.walk(directory):
        for file in files:
            if file.endswith(".py"):
                yield os.path.join(root, file)


class PackageRegistry:
    def __init__(self) -> None:
        self.packages: Dict[str, Package] = {}
        self.categories: List[Category] = []
        self.nodes: Dict[str, Tuple[NodeData, NodeGroup]] = {}

    def get_node(self, schema_id: str) -> NodeData:
        return self.nodes[schema_id][0]

    def add(self, package: Package) -> Package:
        # assert package.where not in self.packages
        self.packages[package.where] = package
        return package

    def load_nodes(self, current_file: str):
        import_errors: List[ImportError] = []

        for package in list(self.packages.values()):
            for file_path in _iter_py_files(os.path.dirname(package.where)):
                _, name = os.path.split(file_path)

                if not name.startswith("_"):
                    module = os.path.relpath(file_path, os.path.dirname(current_file))
                    module = module.replace("/", ".").replace("\\", ".")[: -len(".py")]
                    try:
                        importlib.import_module(module, package=None)
                    except ImportError as e:
                        import_errors.append(e)
                    except RuntimeError as e:
                        logger.warning(f"Failed to load {module}: {e}")
                    except ValueError as e:
                        logger.warning(f"Failed to load {module}: {e}")

        logger.info(import_errors)
        self._refresh_nodes()

        return import_errors

    def _refresh_nodes(self):
        self.nodes = {}
        self.categories = []

        for package in self.packages.values():
            self.categories.extend(package.categories)
            for category in package.categories:
                for sub in category.node_groups:
                    for node in sub.nodes:
                        if node.schema_id in self.nodes:
                            # print warning
                            pass
                        self.nodes[node.schema_id] = node, sub


registry = PackageRegistry()


def add_package(
    where: str, name: str, description: str, dependencies: List[Dependency]
) -> Package:
    return registry.add(Package(where, name, description, dependencies))


is_mac = sys.platform == "darwin"
is_arm_mac = is_mac and platform.machine() == "arm64"
is_windows = sys.platform == "win32"
is_linux = sys.platform == "linux"
