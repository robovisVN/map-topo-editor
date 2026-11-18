import tkinter as tk
from pathlib import Path
from tempfile import NamedTemporaryFile
from tkinter import filedialog, messagebox, ttk

import yaml
from PIL import Image


def export_map(yaml_path: Path, txt_path: Path) -> int:
    with yaml_path.open(encoding="utf-8-sig") as file:
        metadata = yaml.safe_load(file)

    try:
        image_path = yaml_path.parent / metadata["image"]
        resolution = metadata["resolution"]
        origin_x, origin_y = metadata["origin"][:2]
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("File YAML cần có image, resolution và origin hợp lệ.") from error

    if txt_path.resolve() in (yaml_path.resolve(), image_path.resolve()):
        raise ValueError("Hãy chọn tên TXT khác với file YAML và ảnh bản đồ.")

    with Image.open(image_path) as source:
        image = source.convert("L")

    width, height = image.size
    pixels = image.load()
    count = 0
    temporary_path = None
    try:
        with NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=txt_path.parent, delete=False
        ) as output:
            temporary_path = Path(output.name)
            for row in range(height):
                y = origin_y + (height - row - 1) * resolution
                for column in range(width):
                    if pixels[column, row] != 0:
                        continue

                    x = origin_x + column * resolution
                    output.write(f"{x:.3f} {y:.3f} 0 0 0\n")
                    count += 1

        temporary_path.replace(txt_path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)

    return count


def main():
    root = tk.Tk()
    root.title("Xuất bản đồ YAML thành TXT")
    root.minsize(560, 150)

    yaml_file = tk.StringVar(master=root)
    txt_file = tk.StringVar(master=root)

    def choose_yaml():
        selected = filedialog.askopenfilename(
            parent=root,
            title="Chọn file YAML của bản đồ",
            filetypes=[("File YAML", ("*.yaml", "*.yml"))],
        )
        if selected:
            yaml_file.set(selected)
            txt_file.set(str(Path(selected).with_suffix(".txt")))

    def choose_txt():
        suggested = Path(txt_file.get() or "map.txt")
        selected = filedialog.asksaveasfilename(
            parent=root,
            title="Chọn vị trí lưu file TXT",
            initialdir=str(suggested.parent),
            initialfile=suggested.name,
            defaultextension=".txt",
            filetypes=[("File TXT", "*.txt")],
            confirmoverwrite=False,
        )
        if selected:
            txt_file.set(selected)

    def run_export():
        if not yaml_file.get() or not txt_file.get():
            messagebox.showwarning(
                "Thiếu đường dẫn", "Hãy chọn file YAML và vị trí lưu TXT.", parent=root
            )
            return

        destination = Path(txt_file.get())
        if destination.exists() and not messagebox.askyesno(
            "Ghi đè file", f"File đã tồn tại. Bạn có muốn ghi đè?\n\n{destination}", parent=root
        ):
            return

        try:
            count = export_map(Path(yaml_file.get()), destination)
        except (OSError, ValueError, TypeError, yaml.YAMLError) as error:
            messagebox.showerror("Xuất file thất bại", str(error), parent=root)
            return

        messagebox.showinfo(
            "Xuất file thành công", f"Đã lưu {count} điểm vào\n{destination}", parent=root
        )

    frame = ttk.Frame(root, padding=16)
    frame.pack(fill="both", expand=True)
    frame.columnconfigure(1, weight=1)

    for row, label, value, button_label, action in (
        (0, "Bản đồ YAML", yaml_file, "Mở YAML…", choose_yaml),
        (1, "Lưu TXT tại", txt_file, "Chọn nơi lưu…", choose_txt),
    ):
        ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=5)
        ttk.Entry(frame, textvariable=value, state="readonly").grid(
            row=row, column=1, sticky="ew", padx=10, pady=5
        )
        ttk.Button(frame, text=button_label, command=action).grid(row=row, column=2, sticky="ew", pady=5)

    ttk.Button(frame, text="Xuất TXT", command=run_export).grid(
        row=2, column=2, sticky="ew", pady=(12, 0)
    )
    root.mainloop()


if __name__ == "__main__":
    main()
