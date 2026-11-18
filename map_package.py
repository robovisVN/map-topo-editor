"""Read ROS map files and write the topology package used by the editor."""

import json
import math
import time
from copy import copy, deepcopy
from functools import cached_property
from io import BytesIO
from pathlib import Path, PureWindowsPath
from tempfile import NamedTemporaryFile
from zipfile import ZIP_DEFLATED, ZipFile

import yaml
from PIL import Image, ImageChops, ImageDraw


with Path(__file__).with_name("topo_defaults.json").open(encoding="utf-8") as file:
    DEFAULTS = json.load(file)

NODE_KEYS = ("x", "y", "type", "content", "name", "isTurn", "shelfIsTurn", "extraTypes")
LINE_KEYS = ("from", "to", "leftWidth", "rightWidth", "startExpandDistance", "endExpandDistance", "path")


class MissingPGM(FileNotFoundError):
    pass


def read_yaml(data):
    metadata = yaml.safe_load(data)
    if not isinstance(metadata, dict) or not isinstance(metadata.get("image"), str) or not metadata["image"].strip():
        raise ValueError("YAML cần có trường image trỏ tới ảnh PGM.")
    return metadata


class MapImage:
    def __init__(self, metadata, pgm_bytes):
        self.metadata = metadata
        self.pgm_bytes = pgm_bytes
        try:
            self.resolution = float(metadata["resolution"])
            self.origin_x, self.origin_y, self.yaw = map(float, metadata["origin"])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("YAML cần có resolution và origin dạng [x, y, yaw].") from error
        if self.resolution <= 0 or not all(map(math.isfinite, (self.resolution, self.origin_x, self.origin_y, self.yaw))):
            raise ValueError("Resolution phải lớn hơn 0 và origin phải là số hữu hạn.")

        if pgm_bytes[:2] not in (b"P2", b"P5"):
            raise ValueError("Ảnh bản đồ phải là file PGM (P2 hoặc P5).")
        with Image.open(BytesIO(pgm_bytes)) as source:
            self.image = source.convert("L")
        self.width, self.height = self.image.size
        self.cos_yaw = math.cos(self.yaw)
        self.sin_yaw = math.sin(self.yaw)

    @classmethod
    def from_yaml(cls, path, image_path=None):
        metadata = read_yaml(path.read_bytes())
        if image_path is None:
            reference = metadata["image"]
            image_path = path.parent / reference
            if not image_path.is_file():
                image_path = path.parent / PureWindowsPath(reference).name
            if not image_path.is_file():
                raise MissingPGM(reference)
        return cls(metadata, image_path.read_bytes())

    def pixel_to_mm(self, pixel_x, pixel_y):
        x, y = self.pixel_to_world(pixel_x, pixel_y)
        return round(x * 1000), round(y * 1000)

    def pixel_to_world(self, pixel_x, pixel_y):
        local_x = pixel_x * self.resolution
        local_y = (self.height - pixel_y) * self.resolution
        x = self.origin_x + self.cos_yaw * local_x - self.sin_yaw * local_y
        y = self.origin_y + self.sin_yaw * local_x + self.cos_yaw * local_y
        return x, y

    def with_world_origin(self, pixel_x, pixel_y, yaw):
        """Place world (0, 0) at the selected image point."""
        x = pixel_x * self.resolution
        y = (self.height - pixel_y) * self.resolution
        cos_yaw, sin_yaw = math.cos(yaw), math.sin(yaw)
        origin = [sin_yaw * y - cos_yaw * x, -sin_yaw * x - cos_yaw * y, yaw]
        return self.with_pose(*origin)

    @cached_property
    def black_pixels(self):
        return self.image.point([255] + [0] * 255)

    def with_pose(self, x, y, yaw):
        if not all(map(math.isfinite, (x, y, yaw))):
            raise ValueError("Gốc và góc bản đồ phải là số hữu hạn.")
        updated = copy(self)
        updated.metadata = {**self.metadata, "origin": [x, y, yaw]}
        updated.origin_x, updated.origin_y, updated.yaw = x, y, yaw
        updated.cos_yaw, updated.sin_yaw = math.cos(yaw), math.sin(yaw)
        return updated

    def transformed_pose(self, dx, dy, angle):
        cosine, sine = math.cos(angle), math.sin(angle)
        return self.with_pose(
            cosine * self.origin_x - sine * self.origin_y + dx,
            sine * self.origin_x + cosine * self.origin_y + dy,
            math.remainder(self.yaw + angle, math.tau),
        )

    def aligned_for_export(self):
        """Resample the edited pose onto a world-axis-aligned grid once at export."""
        if self.yaw == 0:
            return self
        c, s = round(self.cos_yaw, 15), round(self.sin_yaw, 15)
        w, h = self.image.size
        # Rotated footprint relative to the YAML origin, measured in cells.
        left = min(0, c * w) + min(0, -s * h)
        bottom = min(0, s * w) + min(0, c * h)
        width = math.ceil(abs(c) * w + abs(s) * h)
        height = math.ceil(abs(s) * w + abs(c) * h)
        top = bottom + height
        # Pillow maps output image coordinates (Y down) back into the source.
        inverse = (c, -s, c * left + s * top, s, c, h + s * left - c * top)
        free = float(self.metadata.get("free_thresh", 0.196))
        occupied = float(self.metadata.get("occupied_thresh", 0.65))
        probability = (free + occupied) / 2
        inverted = self.metadata.get("negate", 0) in (1, "1")
        fill = round(255 * (probability if inverted else 1 - probability))
        if self.metadata.get("mode") == "raw":
            fill = 255
        image = self.image.transform((width, height), Image.Transform.AFFINE, inverse,
                                     Image.Resampling.NEAREST, fillcolor=fill)
        origin = [self.origin_x + left * self.resolution,
                  self.origin_y + bottom * self.resolution, 0.0]
        return self.with_raster(image, {**self.metadata, "origin": origin})

    @staticmethod
    def validate_polygon(vertices):
        points = list(dict.fromkeys(vertices))
        if len(points) < 3:
            raise ValueError("Đa giác cần ít nhất ba đỉnh khác nhau.")
        (x0, y0), (x1, y1) = points[:2]
        if not any(abs((x1 - x0) * (y - y0) - (y1 - y0) * (x - x0)) > 1e-9
                   for x, y in points[2:]):
            raise ValueError("Các đỉnh của đa giác không được nằm trên cùng một đường thẳng.")

    def with_erased_polygon(self, vertices, outside):
        """Rasterize the polygon within the image and erase the chosen side."""
        self.validate_polygon(vertices)
        mask = Image.new("L", self.image.size)
        ImageDraw.Draw(mask).polygon(vertices, fill=255)
        if outside:
            if mask.getbbox() is None:
                raise ValueError("Vùng cần giữ phải giao với bản đồ.")
            mode = str(self.metadata.get("mode", "trinary")).lower()
            fill = 205
            if mode == "raw":
                known = [255 if value <= 100 else 0 for value in range(256)]
                fill = 255
            elif mode == "scale":
                # An opaque PGM can encode intermediate occupancy at every grey level.
                known = [255] * 256
            else:
                free = float(self.metadata.get("free_thresh", 0.196))
                occupied = float(self.metadata.get("occupied_thresh", 0.65))
                inverted = self.metadata.get("negate", 0) in (1, "1")
                known = []
                for value in range(256):
                    probability = (value if inverted else 255 - value) / 255
                    known.append(0 if free <= probability <= occupied else 255)
                if known[fill]:
                    unknown_values = [i for i, flag in enumerate(known) if not flag]
                    if not unknown_values:
                        raise ValueError("Ngưỡng YAML không có mức xám biểu diễn ô chưa xác định.")
                    fill = unknown_values[len(unknown_values) // 2]
            bounds = ImageChops.darker(mask, self.image.point(known)).getbbox()
            if bounds is None:
                raise ValueError("Vùng chọn chỉ có ô chưa xác định. Hãy chọn cả phần bản đồ cần giữ.")
            image = Image.new("L", (bounds[2] - bounds[0], bounds[3] - bounds[1]), fill)
            image.paste(self.image.crop(bounds), (0, 0), mask.crop(bounds))
            if image.size != self.image.size:
                x, y = self.pixel_to_world(bounds[0], bounds[3])
                metadata = {**self.metadata, "origin": [x, y, self.yaw],
                            "width": image.width, "height": image.height}
                return self.with_raster(image, metadata)
        else:
            image = self.image.copy()
            free_pixel = 0 if self.metadata.get("negate", 0) in (1, "1") else 255
            image.paste(free_pixel, (0, 0, self.width, self.height), mask)
        if image.tobytes() == self.image.tobytes():
            return self
        return self.with_raster(image, self.metadata)

    def with_raster(self, image, metadata):
        output = BytesIO()
        image.save(output, format="PPM")
        return MapImage(metadata, output.getvalue())

    def mm_to_pixel(self, x, y):
        return self.world_to_pixel(x / 1000, y / 1000)

    def world_to_pixel(self, x, y):
        dx, dy = x - self.origin_x, y - self.origin_y
        local_x = self.cos_yaw * dx + self.sin_yaw * dy
        local_y = -self.sin_yaw * dx + self.cos_yaw * dy
        return local_x / self.resolution, self.height - local_y / self.resolution

    def map_dimensions(self):
        return {
            "width": round(self.width * self.resolution * 1000),
            "height": round(self.height * self.resolution * 1000),
            "xAttrMin": round(self.origin_x * 1000),
            "yAttrMin": round(self.origin_y * 1000),
        }


def point(node):
    return node["coordinate"]["x"], node["coordinate"]["y"]


class Topology:
    def __init__(self, data=None):
        if data is None:
            data = {
                "map": deepcopy(DEFAULTS["map"]), "nodes": [], "attrArea": [],
                "relatingAreas": [], "virtualPoint": [], "guidelineConfigs": deepcopy(DEFAULTS["guidelineConfigs"]),
            }
        if not isinstance(data, dict) or not isinstance(data.get("map"), dict) or not isinstance(data.get("nodes"), list):
            raise ValueError("topo.json cần có map và danh sách nodes.")

        self.data = data
        self.nodes = {}
        try:
            for node in data["nodes"]:
                node_id = node["id"]
                x, y = point(node)
                if not isinstance(node_id, str) or not node_id or node_id in self.nodes:
                    raise ValueError("ID node phải là chuỗi duy nhất.")
                if not all(isinstance(value, int) for value in (x, y)):
                    raise ValueError("Tọa độ node phải là số nguyên theo mm.")
                if not isinstance(node["contentEdges"], list):
                    raise TypeError("contentEdges")
                self.nodes[node_id] = node
            for node in self.nodes.values():
                targets = [edge["destination"] for edge in node["contentEdges"]]
                if len(targets) != len(set(targets)) or any(target not in self.nodes or target == node["id"] for target in targets):
                    raise ValueError("Cạnh topo bị trùng, trỏ vào chính nó hoặc trỏ đến node không tồn tại.")
        except (KeyError, TypeError) as error:
            raise ValueError("Dữ liệu node hoặc cạnh trong topo.json không đúng cấu trúc ZIP mẫu.") from error

        self.next_id = max((int(key) for key in self.nodes if key.isascii() and key.isdecimal()), default=10000000) + 1
        self.next_id = max(self.next_id, 10000001)

    def add_node(self, x, y):
        if any(point(node) == (x, y) for node in self.nodes.values()):
            raise ValueError("Đã có node ở tọa độ này.")
        node_id = str(self.next_id)
        self.next_id += 1
        self.nodes[node_id] = {
            **deepcopy(DEFAULTS["node"]), "id": node_id, "name": node_id, "content": node_id,
            "coordinate": {"x": x, "y": y}, "contentEdges": [], "edges": [],
        }
        return node_id

    def aspect(self, source, target):
        x1, y1 = point(self.nodes[source])
        x2, y2 = point(self.nodes[target])
        dx, dy = x2 - x1, y2 - y1
        if abs(dy) >= abs(dx):
            return "0" if dy > 0 else "1"
        return "2" if dx < 0 else "3"

    def move_node(self, node_id, x, y):
        if any(key != node_id and point(node) == (x, y) for key, node in self.nodes.items()):
            raise ValueError("Đã có node ở tọa độ này.")
        self.nodes[node_id]["coordinate"] = {"x": x, "y": y}
        for source, edge in self.iter_edges():
            target = edge["destination"]
            if node_id in (source, target):
                edge["aspect"] = self.aspect(source, target)

    def rebase(self, old_map, new_map):
        coordinates = {}
        for node_id, node in self.nodes.items():
            pixel = old_map.mm_to_pixel(*point(node))
            coordinates[node_id] = new_map.pixel_to_mm(*pixel)
        # Detect new collisions even if the imported topology already has overlapping nodes.
        old_by_new = {}
        for node_id, coordinate in coordinates.items():
            old_point = point(self.nodes[node_id])
            if old_by_new.setdefault(coordinate, old_point) != old_point:
                raise ValueError("Phép biến đổi làm các node trùng tọa độ sau khi làm tròn mm.")
        for node_id, (x, y) in coordinates.items():
            self.nodes[node_id]["coordinate"] = {"x": x, "y": y}
        for source, edge in self.iter_edges():
            edge["aspect"] = self.aspect(source, edge["destination"])

    def iter_edges(self):
        for node_id, node in self.nodes.items():
            for edge in node["contentEdges"]:
                yield node_id, edge

    def connect(self, source, target, both_ways):
        if source == target:
            raise ValueError("Hãy chọn hai node khác nhau.")
        pairs = ((source, target), (target, source)) if both_ways else ((source, target),)
        count = 0
        for start, end in pairs:
            edges = self.nodes[start]["contentEdges"]
            if any(edge["destination"] == end for edge in edges):
                continue
            edges.append({**deepcopy(DEFAULTS["edge"]), "destination": end, "aspect": self.aspect(start, end)})
            count += 1
        return count

    def disconnect(self, source, target):
        node = self.nodes[source]
        node["contentEdges"] = [edge for edge in node["contentEdges"] if edge["destination"] != target]

    def remove_node(self, node_id):
        del self.nodes[node_id]
        for source in self.nodes:
            self.disconnect(source, node_id)

    def to_json_data(self, name, map_image):
        metadata = {
            **self.data["map"], **map_image.map_dimensions(), "name": name, "timeStamp": int(time.time() * 1000),
        }
        nodes = []
        for node in self.nodes.values():
            edges = [{"destination": edge["destination"], "weight": edge.get("weight", 1)} for edge in node["contentEdges"]]
            nodes.append({**node, "edges": edges})
        return {**self.data, "map": metadata, "nodes": nodes}


def compressed_topology(topo):
    nodes = {node["id"]: node for node in topo["nodes"]}
    node_rows = [[*point(node), *(node[key] for key in NODE_KEYS[2:])] for node in nodes.values()]
    line_rows, all_paths, seen = [], [], set()
    for source, node in nodes.items():
        for edge in node["contentEdges"]:
            target = edge["destination"]
            path = [list(point(node)), list(point(nodes[target]))]
            line_rows.append([source, target, *(edge.get(key, -1) for key in LINE_KEYS[2:-1]), path])
            pair = frozenset((source, target))
            if pair not in seen:
                all_paths.append(path[::-1])
                seen.add(pair)
    return {
        "nodeKeys": NODE_KEYS, "lineKeys": LINE_KEYS, "nodeArr": node_rows, "lineArr": line_rows,
        "allPath": all_paths, "chargeCoor": [],
        **{key: topo["map"][key] for key in ("type", "height", "width", "xAttrMin", "yAttrMin")},
    }


def read_package(path):
    with ZipFile(path) as archive:
        names = set(archive.namelist())
        if "topo.json" not in names:
            raise ValueError("ZIP cần có topo.json ở thư mục gốc.")
        topology = Topology(json.loads(archive.read("topo.json")))
        yaml_names = sorted(name for name in names if "/" not in name and Path(name).suffix.lower() in (".yaml", ".yml"))
        preferred = (topology.data["map"].get("name"), path.stem)
        yaml_name = next((name for stem in preferred for name in yaml_names if Path(name).stem == stem), None)
        if yaml_name is None and len(yaml_names) == 1:
            yaml_name = yaml_names[0]
        if yaml_name is None:
            raise ValueError("Không xác định được file YAML chính trong ZIP.")
        metadata = read_yaml(archive.read(yaml_name))
        image_name = PureWindowsPath(metadata["image"]).name
        if image_name not in names:
            raise ValueError(f"ZIP thiếu file ảnh {image_name} ở thư mục gốc.")
        return MapImage(metadata, archive.read(image_name)), topology


def write_package(path, map_image, topology):
    name = path.stem
    map_image = map_image.aligned_for_export()
    topo = topology.to_json_data(name, map_image)
    compressed = compressed_topology(topo)
    metadata = {**map_image.metadata, "image": f"./{name}.pgm", "width": map_image.width, "height": map_image.height}
    preview = BytesIO()
    map_image.image.save(preview, format="JPEG", quality=90)

    temporary_path = None
    try:
        with NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
            temporary_path = Path(temporary.name)
        with ZipFile(temporary_path, "w", compression=ZIP_DEFLATED) as archive:
            archive.writestr(f"{name}.pgm", map_image.pgm_bytes)
            archive.writestr(f"{name}.yaml", yaml.safe_dump(
                metadata, sort_keys=False, allow_unicode=True, default_flow_style=None, width=4096,
            ))
            archive.writestr("topo.json", json.dumps(topo, ensure_ascii=False, indent=2, allow_nan=False))
            archive.writestr("compress/compress.json", json.dumps(compressed, ensure_ascii=False, separators=(",", ":"), allow_nan=False))
            archive.writestr(f"{name}.jpg", preview.getvalue())
        temporary_path.replace(path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
