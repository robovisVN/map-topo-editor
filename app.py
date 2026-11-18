import math
import tkinter as tk
from copy import deepcopy
from pathlib import Path
from tkinter import filedialog, font, messagebox, simpledialog, ttk
from zipfile import BadZipFile

import yaml
from PIL import Image, ImageDraw, ImageTk

from map_package import MapImage, MissingPGM, Topology, point, read_package, write_package


FILE_ERRORS = (BadZipFile, OSError, ValueError, yaml.YAMLError)
MAP_BACKGROUND = "#ffffff"
MAP_POINT = "#27383b"
MAP_FRAME = "#ae8009"
NODE_SNAP_PIXELS = 8
MODES = {
    "view": "Chọn nhóm công cụ để chỉnh sửa bản đồ.",
    "select": "Bấm hoặc kéo một node để chọn hoặc di chuyển.",
    "add": "Di chuột để căn X/Y theo node có sẵn. Bấm để đặt node tại giao điểm.",
    "connect": "Bấm node nguồn, sau đó bấm node đích.",
    "origin": "Bấm điểm trên ảnh muốn đặt làm gốc (0, 0).",
}


class NodePositionDialog(simpledialog.Dialog):
    def __init__(self, parent, node_id, coordinates, move_node):
        self.node_id = node_id
        self.coordinates = coordinates
        self.move_node = move_node
        super().__init__(parent, title=f"Sửa tọa độ node {node_id}")

    def body(self, master):
        self.entries = []
        for row, (axis, value) in enumerate(zip(("X (mm)", "Y (mm)"), self.coordinates)):
            ttk.Label(master, text=axis).grid(row=row, column=0, sticky="w", padx=(4, 12), pady=4)
            entry = ttk.Entry(master, width=18)
            entry.insert(0, str(value))
            entry.grid(row=row, column=1, pady=4, padx=(0, 4))
            self.entries.append(entry)
        self.entries[0].selection_range(0, tk.END)
        return self.entries[0]

    def buttonbox(self):
        buttons = ttk.Frame(self, padding=(8, 4, 8, 8))
        buttons.pack()
        ttk.Button(buttons, text="Lưu", command=self.ok).pack(side="left", padx=4)
        ttk.Button(buttons, text="Hủy", command=self.cancel).pack(side="left", padx=4)
        self.bind("<Return>", self.ok)
        self.bind("<Escape>", self.cancel)

    def validate(self):
        try:
            x, y = (int(entry.get()) for entry in self.entries)
        except ValueError:
            messagebox.showerror("Tọa độ không hợp lệ", "Nhập X và Y dưới dạng số nguyên theo milimét.", parent=self)
            return False

        if (x, y) != self.coordinates:
            try:
                self.move_node(self.node_id, x, y)
            except ValueError as error:
                messagebox.showerror("Tọa độ không hợp lệ", str(error), parent=self)
                return False
        self.result = (x, y)
        return True


class MapEditor:
    def __init__(self, root):
        self.root = root
        root.title("Map Topo Editor")
        root.geometry("1160x740")
        root.minsize(820, 530)
        root.protocol("WM_DELETE_WINDOW", self.close)

        self.map_image = None
        self.topology = Topology()
        self.source = None
        self.dirty = False
        self.selected = None
        self.connection_start = None
        self.drag_start = None
        self.drag_position = None
        self.drag_guide = None
        self.edit_group = None
        self.edit_snapshot = None
        self.erase_vertices = []
        self.erase_closed = False
        self.erase_selecting = False
        self.erase_anchor = None
        self.photo = None
        self.scale = 1.0
        self.fit_scale = 1.0
        self.view_offset = (0.0, 0.0)
        self.pan_start = None
        self.pan_moved = False

        self.mode = tk.StringVar(value="view")
        self.both_ways = tk.BooleanVar(value=True)
        self.map_name = tk.StringVar(value="Chưa mở bản đồ")
        self.yaml_origin = tk.StringVar()
        self.summary = tk.StringVar(value="0 node · 0 cạnh")
        self.status = tk.StringVar(value="Mở file YAML để bắt đầu.")
        self.cursor_position = tk.StringVar()
        self.build_ui()

    def build_ui(self):
        families = set(font.families(self.root))
        family = next((name for name in ("Segoe UI", "Noto Sans", "DejaVu Sans") if name in families), None)
        if family:
            for name in ("TkDefaultFont", "TkTextFont", "TkHeadingFont"):
                font.nametofont(name).configure(family=family)
        self.node_font = font.nametofont("TkDefaultFont")
        self.node_font.configure(size=10)
        self.emphasis_font = self.node_font.copy()
        self.emphasis_font.configure(weight="bold")
        style = ttk.Style(self.root)
        style.theme_use("clam")
        self.root.configure(background="#e6edf1")
        style.configure(".", background="#ffffff", foreground="#13212d", font=self.node_font, bordercolor="#c8d6d9",
                        focuscolor="#0e85c4")
        style.configure("Toolbar.TFrame", background="#364956")
        style.configure("Toolbar.TLabel", background="#364956", foreground="white", font=self.emphasis_font)
        style.configure("Sidebar.TFrame", background="white", relief="solid", borderwidth=1)
        style.configure("Treeview", fieldbackground="white", rowheight=self.node_font.metrics("linespace") + 9)
        style.configure("Treeview.Heading", background="#364956", foreground="#ffffff",
                        font=self.emphasis_font, padding=(4, 5))
        style.map("Treeview.Heading", background=[("active", "#456477")], foreground=[("active", "white")])
        style.map("Treeview", background=[("selected", "#086793")], foreground=[("selected", "#ffffff")])
        self.button_images = []
        for name, normal, border, hover, pressed, ink in (
            ("TButton", "#edf7fc", "#5385a0", "#cceaf8", "#a8dced", "#12394f"),
            ("Accent.TButton", "#086793", "#086793", "#075779", "#064661", "#ffffff"),
        ):
            def rounded_tile(fill, outline):
                tile = Image.new("RGBA", (84, 84))
                ImageDraw.Draw(tile).rounded_rectangle((1, 1, 82, 82), radius=24,
                                                       fill=fill, outline=outline, width=3)
                photo = ImageTk.PhotoImage(tile.resize((28, 28), Image.Resampling.LANCZOS), master=self.root)
                self.button_images.append(photo)
                return photo

            element = "Local." + name + ".surface"
            style.element_create(element, "image", rounded_tile(normal, border),
                                 ("disabled", "selected", rounded_tile("#364956", "#364956")),
                                 ("disabled", rounded_tile("#eef0f1", "#e3e6e8")),
                                 ("pressed", rounded_tile(pressed, border)),
                                 ("active", rounded_tile(hover, border)), border=9, sticky="nsew")
            style.layout(name, [(element, {"sticky": "nsew", "children": [
                ("Button.focus", {"sticky": "nsew", "children": [
                    ("Button.padding", {"sticky": "nsew", "children": [
                        ("Button.label", {"sticky": "nsew"})]})]})]})])
            style.configure(name, foreground=ink, font=self.emphasis_font, padding=(5, 2), anchor="center")
            style.map(name, foreground=[("disabled", "selected", "#ffffff"),
                                         ("disabled", "#646e74"), ("!disabled", ink)],
                      font=[("disabled", "selected", self.emphasis_font), ("disabled", self.node_font)])

        files = ttk.Frame(self.root, style="Toolbar.TFrame", padding=(12, 9))
        files.pack(fill="x", padx=10, pady=(9, 0))
        tk.Frame(self.root, background="#24a3d6", height=3).pack(fill="x", padx=10, pady=(0, 5))
        self.open_yaml_button = ttk.Button(files, text="Mở YAML…", command=self.open_yaml)
        self.open_yaml_button.pack(side="left")
        self.open_zip_button = ttk.Button(files, text="Mở ZIP…", command=self.open_zip)
        self.open_zip_button.pack(side="left", padx=(8, 14))
        self.export_button = ttk.Button(files, text="Xuất ZIP…", style="Accent.TButton", command=self.export_zip)
        self.export_button.pack(side="right")
        ttk.Label(files, textvariable=self.map_name, style="Toolbar.TLabel").pack(side="left")
        self.export_button.state(["disabled"])

        tools = ttk.Frame(self.root, padding=(12, 6))
        tools.pack(fill="x", padx=10)
        self.group_buttons = {}
        for key, label in (("topo", "Topo"), ("pose", "Xoay / tịnh tiến"), ("erase", "Xóa vùng")):
            # Reserve room for the bold label even when Tk measures the disabled font.
            digit_width = min(self.node_font.measure("0"), self.emphasis_font.measure("0"))
            min_chars = math.ceil(self.emphasis_font.measure(label) / digit_width) + 2
            button = ttk.Button(tools, text=label, width=-min_chars,
                                command=lambda group=key: self.begin_edit(group))
            button.pack(side="left", padx=(0, 6))
            self.group_buttons[key] = button
        ttk.Button(tools, text="Vừa khung", command=self.fit_map).pack(side="right")
        self.cancel_button = ttk.Button(tools, text="✕ Hủy", command=lambda: self.finish_edit(False))
        self.cancel_button.pack(side="right", padx=6)
        self.apply_button = ttk.Button(tools, text="✓ Áp dụng", style="Accent.TButton",
                                       command=lambda: self.finish_edit(True))
        self.apply_button.pack(side="right")

        panels = ttk.Frame(self.root, padding=(12, 0, 12, 6))
        panels.pack(fill="x", padx=10, pady=(0, 6))
        self.edit_panels = {key: ttk.Frame(panels) for key in self.group_buttons}
        topo_tools = self.edit_panels["topo"]
        for label, mode in (("Chọn / di chuyển", "select"), ("Thêm node", "add"), ("Nối node", "connect")):
            ttk.Radiobutton(topo_tools, text=label, variable=self.mode, value=mode,
                            command=self.change_mode).pack(side="left", padx=(0, 12))
        ttk.Checkbutton(topo_tools, text="Nối hai chiều", variable=self.both_ways).pack(side="left")

        erase_tools = self.edit_panels["erase"]
        self.erase_shape = tk.StringVar(value="Đa giác")
        shape_picker = ttk.Combobox(erase_tools, state="readonly", width=12,
                                    textvariable=self.erase_shape, values=("Đa giác", "Chữ nhật"))
        shape_picker.pack(side="left", padx=(0, 8))
        shape_picker.bind("<<ComboboxSelected>>", self.change_erase_shape)
        self.start_region_button = ttk.Button(erase_tools, text="Bắt đầu chọn",
                                              command=self.start_erase_selection)
        self.start_region_button.pack(side="left", padx=(0, 6))
        self.cancel_region_button = ttk.Button(erase_tools, text="Hủy vùng",
                                               command=self.cancel_erase_selection)
        self.cancel_region_button.pack(side="left", padx=(0, 16))
        self.erase_buttons = []
        for label, outside in (("Xóa trong", False), ("Xóa ngoài", True)):
            button = ttk.Button(erase_tools, text=label,
                                command=lambda external=outside: self.erase_region(external))
            button.pack(side="left", padx=(0, 6))
            self.erase_buttons.append(button)

        pose_tools = self.edit_panels["pose"]
        self.translation_step = tk.StringVar(value="0.10")
        self.rotation_step = tk.StringVar(value="0.10")
        ttk.Label(pose_tools, text="Bước dịch (m)").pack(side="left")
        ttk.Entry(pose_tools, textvariable=self.translation_step, width=7).pack(side="left", padx=(4, 6))
        for label, dx, dy in (("←", -1, 0), ("→", 1, 0), ("↑", 0, 1), ("↓", 0, -1)):
            button = ttk.Button(pose_tools, text=label, width=3,
                                command=lambda x=dx, y=dy: self.nudge_map(dx=x, dy=y))
            button.pack(side="left")
        ttk.Label(pose_tools, text="Bước xoay (°)").pack(side="left", padx=(14, 4))
        ttk.Entry(pose_tools, textvariable=self.rotation_step, width=7).pack(side="left", padx=(0, 6))
        for label, direction in (("↶", 1), ("↷", -1)):
            button = ttk.Button(pose_tools, text=label, width=3,
                                command=lambda turn=direction: self.nudge_map(turn=turn))
            button.pack(side="left")
        ttk.Radiobutton(pose_tools, text="Đặt gốc", variable=self.mode, value="origin",
                        command=self.change_mode).pack(side="left", padx=8)
        self.refresh_edit_controls()

        footer = ttk.Frame(self.root, padding=(12, 7))
        footer.pack(side="bottom", fill="x")
        with Image.open(Path(__file__).with_name("robovis-logo.png")) as source:
            logo = source.convert("RGBA")
        logo.thumbnail((82, 18), Image.Resampling.LANCZOS)
        self.brand_logo = ImageTk.PhotoImage(logo, master=self.root)
        ttk.Label(footer, image=self.brand_logo).pack(side="right", padx=(12, 0))
        ttk.Label(footer, textvariable=self.status).pack(side="left")
        ttk.Label(footer, textvariable=self.cursor_position).pack(side="right")

        body = ttk.Frame(self.root)
        body.pack(fill="both", expand=True, padx=12)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)
        self.canvas = tk.Canvas(body, background=MAP_BACKGROUND, highlightthickness=0, takefocus=True)
        self.canvas.grid(row=0, column=0, sticky="nsew")

        sidebar = ttk.Frame(body, style="Sidebar.TFrame", padding=(10, 8))
        sidebar.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        ttk.Label(sidebar, textvariable=self.yaml_origin, foreground="#36566b", wraplength=246).pack(anchor="w")
        ttk.Label(sidebar, textvariable=self.summary).pack(anchor="w", pady=(2, 6))
        node_frame = ttk.Frame(sidebar)
        node_frame.pack(fill="both", expand=True)
        self.node_list = ttk.Treeview(node_frame, columns=("id", "x", "y"), show="headings", selectmode="browse", height=8)
        for key, label, width in (("id", "Node", 90), ("x", "X (mm)", 72), ("y", "Y (mm)", 72)):
            self.node_list.heading(key, text=label)
            self.node_list.column(key, width=width, minwidth=width, stretch=False, anchor="center")
        node_scroll = ttk.Scrollbar(node_frame, orient="vertical", command=self.node_list.yview)
        self.node_list.configure(yscrollcommand=node_scroll.set)
        node_scroll.pack(side="right", fill="y")
        self.node_list.pack(fill="both", expand=True)
        self.node_list.bind("<<TreeviewSelect>>", self.on_node_list)
        self.node_list.bind("<Double-1>", self.on_node_double_click)
        node_actions = ttk.Frame(sidebar)
        node_actions.pack(anchor="e", pady=(6, 12))
        self.edit_node_button = ttk.Button(node_actions, text="Sửa X/Y…", command=self.edit_node_position)
        self.edit_node_button.pack(side="left", padx=(0, 6))
        self.delete_node_button = ttk.Button(node_actions, text="Xóa node", command=self.delete_node)
        self.delete_node_button.pack(side="left")

        ttk.Label(sidebar, text="Cạnh đi ra từ node đã chọn").pack(anchor="w", pady=(0, 5))
        edge_frame = ttk.Frame(sidebar)
        edge_frame.pack(fill="x")
        self.edge_list = tk.Listbox(edge_frame, height=5, exportselection=False, borderwidth=0, highlightthickness=1,
                                    highlightbackground="#b9c4cf", selectbackground="#086793", selectforeground="white")
        edge_scroll = ttk.Scrollbar(edge_frame, orient="vertical", command=self.edge_list.yview)
        self.edge_list.configure(yscrollcommand=edge_scroll.set)
        edge_scroll.pack(side="right", fill="y")
        self.edge_list.pack(fill="x", expand=True)
        self.edge_list.bind("<<ListboxSelect>>", self.update_edge_button)
        self.delete_edge_button = ttk.Button(sidebar, text="Xóa cạnh", command=self.delete_edge)
        self.delete_edge_button.pack(anchor="e", pady=(6, 10))
        ttk.Label(sidebar, text="Kéo chuột phải để dịch góc nhìn. Cuộn để zoom tại con trỏ.",
                  foreground="#45566a",
                  wraplength=246).pack(anchor="w")
        self.refresh_selection()

        for sequence, handler in (
            ("<Configure>", self.on_canvas_resize), ("<Button-1>", self.on_click),
            ("<Button-3>", self.begin_pan), ("<B3-Motion>", self.pan_view),
            ("<ButtonRelease-3>", self.end_pan),
            ("<B1-Motion>", self.on_drag), ("<ButtonRelease-1>", self.end_drag),
            ("<MouseWheel>", self.on_zoom), ("<Button-4>", self.on_zoom), ("<Button-5>", self.on_zoom),
            ("<Motion>", self.on_pointer), ("<Leave>", self.clear_pointer),
        ):
            self.canvas.bind(sequence, handler)
        self.root.bind("<Escape>", lambda _: self.finish_edit(False))

    def may_discard(self):
        return not (self.dirty or self.edit_group) or messagebox.askyesno(
            "Thay đổi chưa xuất", "Các thay đổi chưa được lưu thành ZIP. Bạn muốn bỏ các thay đổi này?", parent=self.root
        )

    def close(self):
        if self.may_discard():
            self.root.destroy()

    def show_error(self, title, error):
        messagebox.showerror(title, str(error), parent=self.root)

    def open_yaml(self):
        if self.edit_group:
            return
        filename = filedialog.askopenfilename(parent=self.root, title="Mở bản đồ YAML", filetypes=[("Bản đồ YAML", ("*.yaml", "*.yml"))])
        if not filename or not self.may_discard():
            return
        path = Path(filename)
        try:
            try:
                map_image = MapImage.from_yaml(path)
            except MissingPGM as missing:
                image = filedialog.askopenfilename(
                    parent=self.root, title=f"Chọn ảnh PGM cho {path.name}", initialdir=path.parent,
                    initialfile=Path(str(missing).replace("\\", "/")).name, filetypes=[("Ảnh PGM", "*.pgm")],
                )
                if not image:
                    return
                map_image = MapImage.from_yaml(path, Path(image))
            self.load(path, map_image, Topology())
        except FILE_ERRORS as error:
            self.show_error("Không mở được bản đồ", error)

    def open_zip(self):
        if self.edit_group:
            return
        filename = filedialog.askopenfilename(parent=self.root, title="Mở ZIP bản đồ", filetypes=[("Bản đồ ZIP", "*.zip")])
        if not filename or not self.may_discard():
            return
        path = Path(filename)
        try:
            self.load(path, *read_package(path))
        except FILE_ERRORS as error:
            self.show_error("Không mở được ZIP", error)

    def load(self, path, map_image, topology):
        aligned = map_image.aligned_for_export()
        self.source, self.map_image, self.topology = path, aligned, topology
        self.pose_source = map_image
        self.selected = self.connection_start = self.drag_start = self.drag_position = None
        self.dirty = False
        self.refresh_map_name()
        self.refresh_origin()
        self.refresh_edit_controls()
        self.change_mode()
        self.refresh_nodes()
        self.fit_map()

    def export_zip(self):
        if self.map_image is None or self.edit_group:
            return
        stem = self.source.stem + ("_edited" if self.source.suffix.lower() == ".zip" else "")
        filename = filedialog.asksaveasfilename(
            parent=self.root, title="Lưu ZIP bản đồ", initialdir=self.source.parent, initialfile=f"{stem}.zip",
            defaultextension=".zip", filetypes=[("File ZIP", "*.zip")],
        )
        if not filename:
            return
        destination = Path(filename)
        try:
            if destination.suffix.lower() != ".zip":
                raise ValueError("Tên file xuất cần có đuôi .zip.")
            write_package(destination, self.map_image, self.topology)
        except (BadZipFile, OSError, ValueError, KeyError, TypeError, yaml.YAMLError) as error:
            self.show_error("Không xuất được ZIP", error)
            return
        self.dirty = False
        self.status.set(f"Đã lưu {destination.name}.")
        messagebox.showinfo("Đã xuất bản đồ", str(destination), parent=self.root)

    def refresh_edit_controls(self):
        active = self.edit_group
        for group, button in self.group_buttons.items():
            button.state(["!disabled" if self.map_image and not active else "disabled"])
            button.state(["selected" if group == active else "!selected"])
        for button in (self.open_yaml_button, self.open_zip_button):
            button.state(["disabled" if active else "!disabled"])
        self.export_button.state(["!disabled" if self.map_image and not active else "disabled"])
        self.cancel_button.state(["!disabled" if active else "disabled"])
        can_apply = active and self.dirty and not self.erase_vertices
        self.apply_button.state(["!disabled" if can_apply else "disabled"])
        erasing = active == "erase"
        self.start_region_button.state(["!disabled" if erasing and not self.erase_selecting else "disabled"])
        self.cancel_region_button.state(["!disabled" if erasing and self.erase_selecting else "disabled"])
        for button in self.erase_buttons:
            button.state(["!disabled" if erasing and self.erase_closed else "disabled"])

    def begin_edit(self, group):
        if self.map_image is None or self.edit_group:
            return
        self.edit_snapshot = (self.map_image, self.topology, self.dirty, self.selected,
                              self.scale, self.fit_scale, self.view_offset, self.pose_source)
        if group != "erase":
            self.topology = deepcopy(self.topology)
        self.edit_group = group
        self.dirty = False
        self.edit_panels[group].pack(fill="x")
        self.mode.set("select" if group == "topo" else "view")
        self.refresh_edit_controls()
        self.change_mode()
        self.refresh_selection()
        if group == "pose":
            self.fit_map()
            self.status.set("Dùng các nút mũi tên để tịnh tiến hoặc xoay quanh gốc.")
        elif group == "erase":
            self.status.set("Chọn hình dạng rồi bấm Bắt đầu chọn.")

    def finish_edit(self, apply):
        if not self.edit_group or (apply and self.erase_vertices):
            return
        image, topology, was_dirty, selected, scale, fit_scale, offset, pose_source = self.edit_snapshot
        if apply:
            self.dirty = self.dirty or was_dirty
        else:
            self.map_image, self.topology, self.dirty = image, topology, was_dirty
            self.pose_source = pose_source
            self.selected = selected
        if not apply or self.edit_group == "pose":
            self.scale, self.fit_scale, self.view_offset = scale, fit_scale, offset
        self.edit_panels[self.edit_group].pack_forget()
        self.edit_group = self.edit_snapshot = None
        self.erase_vertices.clear()
        self.erase_closed = False
        self.erase_selecting = False
        self.erase_anchor = None
        self.mode.set("view")
        self.change_mode()
        self.refresh_edit_controls()
        self.refresh_map_name()
        self.refresh_origin()
        self.refresh_nodes()
        self.draw_map()
        self.cursor_position.set("")
        self.status.set("Đã áp dụng thay đổi." if apply else "Đã hủy thao tác.")

    def change_mode(self):
        self.connection_start = self.drag_start = self.drag_position = self.drag_guide = None
        self.pan_start = None
        self.clear_pointer()
        mode = self.mode.get()
        self.canvas.configure(cursor="arrow" if mode in ("view", "select") else "crosshair")
        self.status.set(MODES[mode] if self.map_image else "Mở file YAML để bắt đầu.")
        self.draw_graph()

    def refresh_map_name(self):
        image = self.map_image
        self.map_name.set(f"{self.source.name}  ·  {image.width} × {image.height} px  ·  {image.resolution:g} m/px")

    def refresh_origin(self):
        image = self.map_image
        self.yaml_origin.set(
            f"Gốc YAML: ({image.origin_x * 1000:.12g}, {image.origin_y * 1000:.12g}) mm"
            f"\nGóc YAML: {math.degrees(image.yaw):.8g}°"
            f"\nGóc lưới nguồn: {math.degrees(self.pose_source.yaw):.8g}°"
        )

    def update_pose(self, updated):
        # Always sample the source grid, even across successive pose sessions.
        aligned = updated.aligned_for_export()
        # Node coordinates are integer millimetres. Round once from the
        # session's starting pose so fine rotation steps do not accumulate error.
        original_topology, original_pose = self.edit_snapshot[1], self.edit_snapshot[-1]
        topology = deepcopy(original_topology)
        topology.rebase(original_pose, updated)
        self.topology = topology
        self.pose_source, self.map_image = updated, aligned
        self.refresh_map_name()

    def set_world_origin(self, x, y):
        if self.edit_group != "pose":
            return
        if not (0 <= x <= self.map_image.width and 0 <= y <= self.map_image.height):
            return
        try:
            wx, wy = self.map_image.pixel_to_world(x, y)
            self.update_pose(self.pose_source.transformed_pose(-wx, -wy, 0))
        except (ValueError, OSError, MemoryError, OverflowError) as error:
            self.show_error("Không thể đặt gốc", error)
            return
        self.dirty = True
        self.refresh_origin()
        self.refresh_nodes()
        self.fit_map()
        self.cursor_position.set("")
        self.status.set("Đã đặt gốc (0, 0).")

    def nudge_map(self, dx=0, dy=0, turn=0):
        if self.edit_group != "pose":
            return
        field = self.rotation_step if turn else self.translation_step
        try:
            step = float(field.get().strip().replace(",", "."))
            if not math.isfinite(step) or step <= 0:
                raise ValueError("Bước chỉnh phải là số hữu hạn lớn hơn 0.")
            angle = math.radians(math.remainder(step, 360)) * turn if turn else 0
            self.update_pose(self.pose_source.transformed_pose(dx * step, dy * step, angle))
        except (ValueError, OSError, MemoryError, OverflowError) as error:
            self.show_error("Không thể chỉnh vị trí bản đồ", error)
            return
        self.dirty = True
        self.refresh_origin()
        self.refresh_nodes()
        self.draw_map()
        self.cursor_position.set("")
        self.status.set("Đã xoay quanh gốc (0, 0)." if turn else "Đã tịnh tiến bản đồ.")

    def reset_erase_selection(self, selecting):
        self.erase_vertices.clear()
        self.erase_anchor = None
        self.erase_closed = False
        self.erase_selecting = selecting
        self.canvas.configure(cursor="crosshair" if selecting else "arrow")
        self.refresh_edit_controls()

    def start_erase_selection(self):
        if self.edit_group != "erase":
            return
        self.reset_erase_selection(True)
        self.draw_graph()
        if self.erase_shape.get() == "Chữ nhật":
            self.status.set("Kéo chuột trái để chọn hình chữ nhật.")
        else:
            self.status.set("Chuột trái thêm đỉnh. Chuột phải khép đa giác.")

    def cancel_erase_selection(self):
        self.reset_erase_selection(False)
        self.draw_graph()
        self.status.set("Đã bỏ vùng đang chọn. Bấm Bắt đầu chọn để vẽ lại.")

    def change_erase_shape(self, _):
        if self.erase_selecting:
            self.start_erase_selection()

    def close_polygon(self, _):
        if self.erase_selecting and not self.erase_closed and self.erase_shape.get() == "Đa giác":
            self.close_erase_outline()

    def close_erase_outline(self):
        try:
            self.map_image.validate_polygon(self.erase_vertices)
        except ValueError as error:
            self.status.set(str(error))
            return
        self.erase_closed = True
        self.refresh_edit_controls()
        self.draw_graph()
        self.status.set("Đã chọn vùng. Bấm Xóa trong hoặc Xóa ngoài.")

    def erase_region(self, outside):
        if self.edit_group != "erase" or not self.erase_closed:
            return
        try:
            updated = self.map_image.with_erased_polygon(self.erase_vertices, outside)
        except (ValueError, OSError, MemoryError) as error:
            self.status.set(str(error))
            return
        changed = updated is not self.map_image
        self.map_image = updated
        if changed:
            self.pose_source = updated
        self.dirty = self.dirty or changed
        self.start_erase_selection()
        self.draw_map()
        instruction = self.status.get()
        result = "Đã xóa vùng." if changed else "Vùng này không có pixel cần thay đổi."
        self.status.set(f"{result} {instruction}")

    def update_erase_rectangle(self, event):
        x1, y1 = self.erase_anchor
        x2, y2 = self.screen_to_pixel(event.x, event.y)
        self.erase_vertices[:] = [(x1, y1), (x2, y1), (x2, y2), (x1, y2)]
        self.draw_graph()

    def on_canvas_resize(self, _):
        self.clear_pointer()
        if self.map_image is None:
            self.canvas.delete("placeholder")
            self.canvas.create_text(self.canvas.winfo_width() / 2, self.canvas.winfo_height() / 2,
                                    text="Mở YAML hoặc ZIP để hiển thị bản đồ", fill="#586a75", tags="placeholder")
        else:
            self.draw_map()

    def fit_map(self):
        if self.map_image is None:
            return
        width, height = self.canvas.winfo_width(), self.canvas.winfo_height()
        image = self.map_image
        corners = [image.pixel_to_world(x, y) for x, y in self.map_corners()]
        xs, ys = zip(*corners)
        center_x = center_y = 0
        if self.edit_group != "pose":
            center_x = (min(xs) + max(xs)) / 2
            center_y = (min(ys) + max(ys)) / 2
        extent_x = max(abs(x - center_x) for x in xs)
        extent_y = max(abs(y - center_y) for y in ys)
        self.fit_scale = image.resolution * min(max(1, width - 26) / (2 * extent_x),
                                                max(1, height - 26) / (2 * extent_y))
        self.scale = self.fit_scale
        factor = self.scale / image.resolution
        self.view_offset = (-center_x * factor, center_y * factor)
        self.clear_pointer()
        self.draw_map()

    def map_corners(self):
        width, height = self.map_image.image.size
        return (0, 0), (width, 0), (width, height), (0, height)

    def draw_map(self):
        self.canvas.delete("map", "placeholder")
        corners = [self.pixel_to_screen(x, y) for x, y in self.map_corners()]
        xs, ys = zip(*corners)
        left, top = max(0, math.floor(min(xs))), max(0, math.floor(min(ys)))
        right = min(self.canvas.winfo_width(), math.ceil(max(xs)))
        bottom = min(self.canvas.winfo_height(), math.ceil(max(ys)))
        if right > left and bottom > top:
            image = self.map_image
            px, py = self.screen_to_pixel(left, top)
            cosine, sine = image.cos_yaw / self.scale, image.sin_yaw / self.scale
            mask = image.black_pixels.transform(
                (right - left, bottom - top), Image.Transform.AFFINE,
                (cosine, -sine, px, sine, cosine, py),
                resample=Image.Resampling.NEAREST, fillcolor=0,
            )
            visible = Image.new("RGB", mask.size, MAP_BACKGROUND)
            visible.paste(MAP_POINT, (0, 0, *mask.size), mask)
            self.photo = ImageTk.PhotoImage(visible, master=self.canvas)
            self.canvas.create_image(left, top, image=self.photo, anchor="nw", tags="map")
        self.canvas.create_polygon(*[coordinate for corner in corners for coordinate in corner],
                                   fill="", outline=MAP_FRAME, width=1, tags="map")
        self.draw_graph()

    def screen_origin(self):
        return (self.canvas.winfo_width() / 2 + self.view_offset[0],
                self.canvas.winfo_height() / 2 + self.view_offset[1])

    def screen_to_pixel(self, x, y):
        image = self.map_image
        origin_x, origin_y = self.screen_origin()
        meters = image.resolution / self.scale
        return image.world_to_pixel((x - origin_x) * meters, (origin_y - y) * meters)

    def pixel_to_screen(self, x, y):
        world_x, world_y = self.map_image.pixel_to_world(x, y)
        factor = self.scale / self.map_image.resolution
        origin_x, origin_y = self.screen_origin()
        return origin_x + world_x * factor, origin_y - world_y * factor

    def node_on_screen(self, node_id):
        if self.drag_position is not None and self.drag_start[0] == node_id:
            return self.drag_position
        return self.pixel_to_screen(*self.map_image.mm_to_pixel(*point(self.topology.nodes[node_id])))

    def draw_graph(self):
        self.canvas.delete("graph")
        if self.map_image is None:
            return
        positions = {node_id: self.node_on_screen(node_id) for node_id in self.topology.nodes}
        links = {(source, edge["destination"]) for source, edge in self.topology.iter_edges()}
        for source, target in sorted(links):
            reverse = (target, source) in links
            if reverse and source > target:
                continue
            x1, y1 = positions[source]
            x2, y2 = positions[target]
            length = math.hypot(x2 - x1, y2 - y1)
            if length == 0:
                continue
            inset = min(9 / length, 0.19)
            dx, dy = (x2 - x1) * inset, (y2 - y1) * inset
            arrow = ("both" if reverse else "last") if length >= 25 else "none"
            color = "#086793" if self.selected in (source, target) else "#537d95"
            self.canvas.create_line(x1 + dx, y1 + dy, x2 - dx, y2 - dy, width=2,
                                    fill=color, arrow=arrow, arrowshape=(8, 10, 4), tags="graph")
        for node_id, (x, y) in positions.items():
            selected = node_id in (self.selected, self.connection_start)
            radius = 7 if selected else 5
            color = "#eaab48" if selected else "#0e85c4"
            self.canvas.create_oval(x - radius, y - radius, x + radius, y + radius, fill=color,
                                    outline="white", width=1, tags="graph")
        self.draw_origins()
        vertices = [self.pixel_to_screen(x, y) for x, y in self.erase_vertices]
        if len(vertices) > 1:
            outline = vertices + [vertices[0]] if self.erase_closed or self.erase_anchor else vertices
            self.canvas.create_line(*[value for vertex in outline for value in vertex],
                                    fill="#087cb3", width=2, tags="graph")
        for x, y in vertices:
            self.canvas.create_oval(x - 3, y - 3, x + 3, y + 3, fill="#087cb3",
                                    outline=MAP_BACKGROUND, tags="graph")
        if self.mode.get() == "select" and self.selected in self.topology.nodes:
            if self.drag_position is None:
                coordinate = point(self.topology.nodes[self.selected])
                others = [point(node) for node_id, node in self.topology.nodes.items()
                          if node_id != self.selected]
                aligned = tuple(any(other[axis] == coordinate[axis] for other in others)
                                for axis in range(2))
                self.draw_node_guide(coordinate, aligned, tag="graph")
            elif self.drag_guide is not None:
                self.draw_node_guide(*self.drag_guide, tag="graph")
        self.canvas.tag_raise("node_guide")

    def draw_origins(self):
        image = self.map_image
        markers = [(image.mm_to_pixel(0, 0), "Gốc map (0, 0)", MAP_FRAME, "world_origin")]
        if (image.origin_x, image.origin_y) == (0, 0):
            markers[0] = ((0, image.height), "Gốc map và YAML (0, 0)", MAP_FRAME, "world_origin")
        else:
            markers.append(((0, image.height), "Gốc YAML", "#80549c", "yaml_origin"))

        view_width, view_height = self.canvas.winfo_width(), self.canvas.winfo_height()
        label_height = self.node_font.metrics("linespace") + 6
        for pixel, label, color, tag in markers:
            x, y = self.pixel_to_screen(*pixel)
            if tag == "world_origin":
                x, y = self.screen_origin()
            if not (0 <= x < view_width and 0 <= y < view_height):
                continue
            tags = ("graph", tag)
            radius = 9
            if tag == "world_origin":
                self.canvas.create_oval(x - radius, y - radius, x + radius, y + radius,
                                        outline=color, width=2, tags=tags)
                self.canvas.create_line(x - 4, y, x + 4, y, fill=color, width=2, tags=tags)
                self.canvas.create_line(x, y - 4, x, y + 4, fill=color, width=2, tags=tags)
            else:
                self.canvas.create_polygon(x, y - radius, x + radius, y, x, y + radius, x - radius, y,
                                           fill="", outline=color, width=2, tags=tags)

            label_width = self.node_font.measure(label) + 10
            left = x - label_width - 13 if tag == "world_origin" else x + 13
            if left < 3:
                left = x + 13
            left = max(3, min(left, view_width - label_width - 3))
            top = y + 11 if tag == "world_origin" else y - label_height - 11
            top = min(max(3, top), view_height - label_height - 3)
            box = (left, top, left + label_width, top + label_height)
            self.canvas.create_rectangle(*box, fill=MAP_BACKGROUND, outline=color, tags=tags)
            self.canvas.create_text(left + 5, top + 3, text=label, fill=color,
                                    font=self.node_font, anchor="nw", tags=tags)

    def on_zoom(self, event):
        if self.map_image is None:
            return
        factor = 1.22 if event.num == 4 or event.delta > 0 else 1 / 1.22
        previous_scale = self.scale
        self.scale = max(self.fit_scale * 0.35, min(self.fit_scale * 50, self.scale * factor))
        if self.edit_group != "pose":
            origin_x, origin_y = self.screen_origin()
            ratio = self.scale / previous_scale
            self.view_offset = (
                self.view_offset[0] + (event.x - origin_x) * (1 - ratio),
                self.view_offset[1] + (event.y - origin_y) * (1 - ratio),
            )
        self.draw_map()
        self.on_pointer(event)

    def begin_pan(self, event):
        if self.map_image is not None and self.edit_group != "pose":
            self.pan_start = (event.x, event.y, self.view_offset)
            self.pan_moved = False

    def pan_view(self, event):
        if self.pan_start is None:
            return
        x, y, (offset_x, offset_y) = self.pan_start
        dx, dy = event.x - x, event.y - y
        if not self.pan_moved and math.hypot(dx, dy) < 4:
            return
        self.pan_moved = True
        self.view_offset = (offset_x + dx, offset_y + dy)
        self.draw_map()
        self.on_pointer(event)

    def end_pan(self, event):
        if self.pan_start is None:
            return
        self.pan_view(event)
        self.pan_start = None
        if not self.pan_moved:
            self.close_polygon(event)

    def map_coordinate(self, x, y):
        pixel_x, pixel_y = self.screen_to_pixel(x, y)
        if 0 <= pixel_x < self.map_image.width and 0 <= pixel_y < self.map_image.height:
            return self.map_image.pixel_to_mm(pixel_x, pixel_y)
        return None

    def on_pointer(self, event):
        coordinate = self.map_coordinate(event.x, event.y) if self.map_image else None
        self.canvas.delete("node_guide")
        if coordinate is not None and self.mode.get() == "add":
            coordinate, aligned = self.suggest_node_coordinate(coordinate)
            self.draw_node_guide(coordinate, aligned)
        elif self.drag_guide is not None:
            coordinate, _ = self.drag_guide
        self.cursor_position.set(f"X {coordinate[0]:,} mm   Y {coordinate[1]:,} mm" if coordinate else "")

    def clear_pointer(self, _=None):
        self.canvas.delete("node_guide")
        self.cursor_position.set("")

    def suggest_node_coordinate(self, coordinate, exclude=None):
        x, y = coordinate
        nodes = {point(node) for node_id, node in self.topology.nodes.items() if node_id != exclude}
        image = self.map_image
        tolerance = NODE_SNAP_PIXELS * image.resolution * 1000 / self.scale
        snap_x = min((nx for nx, _ in nodes), key=lambda nx: (abs(nx - x), nx), default=None)
        snap_y = min((ny for _, ny in nodes), key=lambda ny: (abs(ny - y), ny), default=None)
        align_x = snap_x is not None and abs(snap_x - x) <= tolerance
        align_y = snap_y is not None and abs(snap_y - y) <= tolerance
        candidates = []
        if align_x and align_y:
            candidates.append(((snap_x, snap_y), (True, True)))
        single_axis = []
        if align_x:
            single_axis.append(((snap_x, y), (True, False)))
        if align_y:
            single_axis.append(((x, snap_y), (False, True)))
        candidates.extend(sorted(single_axis, key=lambda item: math.dist(coordinate, item[0])))
        for candidate, aligned in candidates:
            px, py = image.mm_to_pixel(*candidate)
            if candidate not in nodes and 0 <= px < image.width and 0 <= py < image.height:
                return candidate, aligned
        return coordinate, (False, False)

    def draw_node_guide(self, coordinate, aligned, tag="node_guide"):
        x, y = self.pixel_to_screen(*self.map_image.mm_to_pixel(*coordinate))
        width, height = self.canvas.winfo_width(), self.canvas.winfo_height()
        for line, snapped in (((x, 0, x, height), aligned[0]), ((0, y, width, y), aligned[1])):
            self.canvas.create_line(*line, fill="#087cb3" if snapped else "#a8bbca",
                                    dash=() if snapped else (4, 4), tags=tag)
        label = f"({coordinate[0]}, {coordinate[1]}) mm"
        label_width = self.node_font.measure(label) + 12
        label_height = self.node_font.metrics("linespace") + 6
        left = max(2, min(x + 8, width - label_width - 2))
        top = max(2, min(y - label_height - 6, height - label_height - 2))
        self.canvas.create_rectangle(left, top, left + label_width, top + label_height,
                                     fill=MAP_BACKGROUND, outline="#a8bbca", tags=tag)
        self.canvas.create_text(left + 6, top + 3, text=label, fill="#253f49",
                                font=self.node_font, anchor="nw", tags=tag)

    def hit_node(self, x, y):
        closest, distance = None, 11
        for node_id in self.topology.nodes:
            nx, ny = self.node_on_screen(node_id)
            current = math.hypot(nx - x, ny - y)
            if current < distance:
                closest, distance = node_id, current
        return closest

    def on_click(self, event):
        if self.map_image is None:
            return
        self.canvas.focus_set()
        mode = self.mode.get()
        if mode == "origin":
            self.set_world_origin(*self.screen_to_pixel(event.x, event.y))
            return
        if self.edit_group == "erase":
            if self.erase_selecting and not self.erase_closed:
                vertex = self.screen_to_pixel(event.x, event.y)
                if self.erase_shape.get() == "Chữ nhật":
                    self.erase_anchor = vertex
                    self.erase_vertices[:] = [vertex]
                elif vertex not in self.erase_vertices:
                    self.erase_vertices.append(vertex)
                self.refresh_edit_controls()
                self.draw_graph()
            return
        if mode == "add":
            coordinate = self.map_coordinate(event.x, event.y)
            if coordinate is None:
                return
            coordinate, _ = self.suggest_node_coordinate(coordinate)
            try:
                self.selected = self.topology.add_node(*coordinate)
            except ValueError as error:
                self.status.set(str(error))
                return
            self.dirty = True
            self.status.set(f"Đã thêm node {self.selected}.")
            self.refresh_nodes()
            return
        node_id = self.hit_node(event.x, event.y)
        if mode == "connect":
            if node_id:
                self.connect_to(node_id)
        else:
            self.select_node(node_id)
            self.drag_start = (node_id, event.x, event.y) if node_id and self.edit_group == "topo" else None

    def connect_to(self, node_id):
        source = self.connection_start
        if source is None:
            self.connection_start = node_id
            self.select_node(node_id)
            self.status.set(f"Đã chọn nguồn {node_id}. Bấm node đích.")
            return
        try:
            count = self.topology.connect(source, node_id, self.both_ways.get())
        except ValueError as error:
            self.status.set(str(error))
            return
        self.connection_start = None
        self.dirty = self.dirty or bool(count)
        self.status.set(f"Đã thêm {count} cạnh giữa {source} và {node_id}." if count else "Kết nối này đã tồn tại.")
        self.refresh_nodes()

    def on_drag(self, event):
        if self.erase_anchor is not None:
            self.update_erase_rectangle(event)
            return
        if self.drag_start is None:
            return
        node_id, start_x, start_y = self.drag_start
        if self.drag_position is None and math.hypot(event.x - start_x, event.y - start_y) < 4:
            return
        self.drag_position = (event.x, event.y)
        self.drag_guide = self.move_node_guide(node_id, event)
        if self.drag_guide is not None:
            coordinate, _ = self.drag_guide
            self.drag_position = self.pixel_to_screen(*self.map_image.mm_to_pixel(*coordinate))
        self.draw_graph()
        self.on_pointer(event)

    def move_node_guide(self, node_id, event):
        coordinate = self.map_coordinate(event.x, event.y)
        if coordinate is None:
            return None
        return self.suggest_node_coordinate(coordinate, exclude=node_id)

    def end_drag(self, event):
        if self.erase_anchor is not None:
            self.update_erase_rectangle(event)
            self.erase_anchor = None
            if len(set(self.erase_vertices)) < 4:
                self.start_erase_selection()
                return
            self.close_erase_outline()
            return
        if self.drag_start is None:
            return
        node_id = self.drag_start[0]
        moved = self.drag_position is not None
        self.drag_start = self.drag_position = self.drag_guide = None
        if not moved:
            return
        guide = self.move_node_guide(node_id, event)
        if guide is None:
            self.status.set("Hãy đặt node trong phạm vi bản đồ.")
            self.draw_graph()
            return
        coordinates, _ = guide
        if coordinates != point(self.topology.nodes[node_id]):
            try:
                self.topology.move_node(node_id, *coordinates)
                self.dirty = True
                self.status.set(f"Đã di chuyển node {node_id}.")
            except ValueError as error:
                self.status.set(str(error))
        self.refresh_nodes()

    def refresh_nodes(self):
        rows = self.node_list.get_children()
        if rows:
            self.node_list.delete(*rows)
        for node_id, node in self.topology.nodes.items():
            self.node_list.insert("", "end", iid=node_id, values=(node_id, *point(node)))
        self.summary.set(f"{len(self.topology.nodes)} node · {sum(1 for _ in self.topology.iter_edges())} cạnh")
        self.refresh_edit_controls()
        self.refresh_selection()

    def on_node_list(self, _):
        rows = self.node_list.selection()
        if rows and rows[0] != self.selected:
            self.select_node(rows[0])

    def on_node_double_click(self, event):
        node_id = self.node_list.identify_row(event.y)
        if node_id:
            self.select_node(node_id)
            self.edit_node_position()
            return "break"

    def edit_node_position(self):
        node_id = self.selected
        if not node_id or self.edit_group != "topo":
            return
        coordinates = point(self.topology.nodes[node_id])
        dialog = NodePositionDialog(self.root, node_id, coordinates, self.topology.move_node)
        if dialog.result is None or dialog.result == coordinates:
            return
        x, y = dialog.result
        self.dirty = True
        self.status.set(f"Đã cập nhật node {node_id} tại X {x} mm, Y {y} mm.")
        self.refresh_nodes()

    def select_node(self, node_id):
        self.selected = node_id
        self.refresh_selection()

    def refresh_selection(self):
        self.edge_list.delete(0, tk.END)
        if self.selected:
            if self.node_list.selection() != (self.selected,):
                self.node_list.selection_set(self.selected)
                self.node_list.see(self.selected)
            for edge in self.topology.nodes[self.selected]["contentEdges"]:
                self.edge_list.insert(tk.END, f"→  {edge['destination']}")
        elif self.node_list.selection():
            self.node_list.selection_remove(*self.node_list.selection())
        node_action_state = ["!disabled" if self.selected and self.edit_group == "topo" else "disabled"]
        self.edit_node_button.state(node_action_state)
        self.delete_node_button.state(node_action_state)
        self.update_edge_button()
        self.draw_graph()

    def update_edge_button(self, _=None):
        can_delete = self.edit_group == "topo" and self.edge_list.curselection()
        self.delete_edge_button.state(["!disabled" if can_delete else "disabled"])

    def delete_edge(self):
        index = self.edge_list.curselection()
        if self.edit_group != "topo" or not self.selected or not index:
            return
        edge = self.topology.nodes[self.selected]["contentEdges"][index[0]]
        self.topology.disconnect(self.selected, edge["destination"])
        self.dirty = True
        self.status.set(f"Đã xóa cạnh {self.selected} → {edge['destination']}.")
        self.refresh_nodes()

    def delete_node(self):
        node_id = self.selected
        if self.edit_group != "topo" or not node_id or not messagebox.askyesno(
            "Xóa node", f"Xóa node {node_id} và các cạnh nối với node này?", parent=self.root
        ):
            return
        self.topology.remove_node(node_id)
        self.selected = self.connection_start = None
        self.dirty = True
        self.status.set(f"Đã xóa node {node_id}.")
        self.refresh_nodes()


if __name__ == "__main__":
    window = tk.Tk()
    MapEditor(window)
    window.mainloop()
