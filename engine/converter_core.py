import io
import json
import math
import os
import struct
import traceback

import numpy as np
from PIL import Image


APP_NAME = "BNMA → GLB Converter"
XOR_KEY = b"H3KWRDKEMS9C4GHCTT86JPZF6UAVOJSZ"


# ----------------------------
# Binary helpers
# ----------------------------

def read_property(buf, pos):
    t = chr(buf[pos])
    pos += 1

    if t == "Y":
        return struct.unpack_from("<h", buf, pos)[0], pos + 2
    if t == "C":
        return bool(buf[pos]), pos + 1
    if t == "I":
        return struct.unpack_from("<i", buf, pos)[0], pos + 4
    if t == "F":
        return struct.unpack_from("<f", buf, pos)[0], pos + 4
    if t == "D":
        return struct.unpack_from("<d", buf, pos)[0], pos + 8
    if t == "L":
        return struct.unpack_from("<q", buf, pos)[0], pos + 8

    if t == "S":
        n = struct.unpack_from("<I", buf, pos)[0]
        pos += 4
        return buf[pos:pos+n].decode("utf-8", "replace"), pos + n

    if t == "R":
        n = struct.unpack_from("<I", buf, pos)[0]
        pos += 4
        return bytes(buf[pos:pos+n]), pos + n

    if t in "fildb":
        n = struct.unpack_from("<I", buf, pos)[0]
        encoding = struct.unpack_from("<I", buf, pos + 4)[0]
        compressed_len = struct.unpack_from("<I", buf, pos + 8)[0]
        pos += 12

        raw = bytes(buf[pos:pos + compressed_len])
        pos += compressed_len

        if encoding == 1:
            import zlib
            raw = zlib.decompress(raw)

        fmt = {
            "f": "<f",
            "i": "<i",
            "l": "<q",
            "d": "<d",
            "b": "<b",
        }[t]

        if not n:
            return [], pos

        return list(struct.unpack("<" + fmt[1] * n, raw)), pos

    raise ValueError(f"Unsupported FBX property type: {t!r}")


def parse_nodes(buf, start=27, end=None):
    """
    FBX 7.4 binary node parser.
    This sample uses FBX version 7400.
    """
    if end is None:
        end = len(buf)

    nodes = []
    pos = start

    while pos + 13 <= end:
        end_offset, prop_count, prop_len = struct.unpack_from("<III", buf, pos)
        name_len = buf[pos + 12]

        # Null record / footer.
        if end_offset == 0 and prop_count == 0 and prop_len == 0 and name_len == 0:
            pos += 13
            break

        if end_offset <= pos or end_offset > len(buf):
            raise ValueError(
                f"Invalid FBX node at 0x{pos:X}: end=0x{end_offset:X}"
            )

        name = buf[pos + 13:pos + 13 + name_len].decode(
            "utf-8", "replace"
        )

        p = pos + 13 + name_len
        props = []

        for _ in range(prop_count):
            value, p = read_property(buf, p)
            props.append(value)

        children = []
        if p < end_offset:
            children, _ = parse_nodes(buf, p, end_offset)

        nodes.append((name, props, children, pos, end_offset))
        pos = end_offset

    return nodes, pos


def find_child(node, name):
    for child in node[2]:
        if child[0] == name:
            return child
    raise KeyError(name)


def normalize_fbx_matrix(row_major):
    """
    FBX matrices in this file contain translation in the last row.
    Convert to conventional column-translation matrices and convert cm→m.
    """
    m = np.asarray(row_major, dtype=np.float64).reshape((4, 4)).T
    m[:3, :3] *= 0.01
    m[:3, 3] *= 0.01
    m[3, :] = [0.0, 0.0, 0.0, 1.0]
    return m


def quaternion_from_matrix(m):
    a = np.asarray(m[:3, :3], dtype=np.float64)
    scale = np.linalg.norm(a, axis=0)
    scale[scale < 1e-12] = 1.0

    r = a / scale

    if np.linalg.det(r) < 0:
        scale[0] *= -1.0
        r[:, 0] *= -1.0

    # Rotation matrix -> quaternion [x, y, z, w]
    trace = float(np.trace(r))

    if trace > 0.0:
        s = math.sqrt(trace + 1.0) * 2.0
        w = 0.25 * s
        x = (r[2, 1] - r[1, 2]) / s
        y = (r[0, 2] - r[2, 0]) / s
        z = (r[1, 0] - r[0, 1]) / s
    elif r[0, 0] > r[1, 1] and r[0, 0] > r[2, 2]:
        s = math.sqrt(1.0 + r[0, 0] - r[1, 1] - r[2, 2]) * 2.0
        w = (r[2, 1] - r[1, 2]) / s
        x = 0.25 * s
        y = (r[0, 1] + r[1, 0]) / s
        z = (r[0, 2] + r[2, 0]) / s
    elif r[1, 1] > r[2, 2]:
        s = math.sqrt(1.0 + r[1, 1] - r[0, 0] - r[2, 2]) * 2.0
        w = (r[0, 2] - r[2, 0]) / s
        x = (r[0, 1] + r[1, 0]) / s
        y = 0.25 * s
        z = (r[1, 2] + r[2, 1]) / s
    else:
        s = math.sqrt(1.0 + r[2, 2] - r[0, 0] - r[1, 1]) * 2.0
        w = (r[1, 0] - r[0, 1]) / s
        x = (r[0, 2] + r[2, 0]) / s
        y = (r[1, 2] + r[2, 1]) / s
        z = 0.25 * s

    q = np.array([x, y, z, w], dtype=np.float64)
    n = np.linalg.norm(q)
    if n > 1e-12:
        q /= n
    return q, scale


# ----------------------------
# BNMA → embedded FBX
# ----------------------------

def decrypt_bnma(data):
    """
    Locate the embedded FBX by trying every 32-byte XOR phase.
    BNMA variants observed so far use the same key but can place the
    encrypted FBX at different offsets.
    """
    if data[:4] != b"BNMA":
        raise ValueError("Это не BNMA: отсутствует сигнатура BNMA.")

    target = b"Kaydara FBX Binary"
    hit = None

    # Search the whole container. The first 18-byte FBX signature is strong
    # enough to avoid false positives.
    for p in range(0x40, len(data) - len(target)):
        c0 = data[p]
        for phase in range(32):
            if c0 ^ XOR_KEY[phase] != target[0]:
                continue
            if all(
                (data[p + j] ^ XOR_KEY[(phase + j) % 32]) == target[j]
                for j in range(len(target))
            ):
                hit = (p, phase)
                break
        if hit:
            break

    if hit is None:
        raise ValueError(
            "Не найден встроенный FBX. "
            "Формат BNMA этой версии не распознан."
        )

    start, phase = hit
    dds_offset = data.find(b"DDS ", start + 27)

    if dds_offset < 0:
        dds_offset = len(data)

    encrypted_model = data[start:dds_offset]
    model = bytearray(encrypted_model)

    for i in range(len(model)):
        model[i] ^= XOR_KEY[(phase + i) % 32]

    if not bytes(model).startswith(b"Kaydara FBX Binary"):
        raise ValueError("Ошибка XOR-декодирования FBX.")

    return bytes(model), data[dds_offset:], start


# ----------------------------
# FBX mesh extraction
# ----------------------------

def extract_scene(fbx_data, dds_data, log=None, fast_texture=True):
    if log:
        log("Читаю FBX 7.4...")

    version = struct.unpack_from("<I", fbx_data, 23)[0]
    if version < 7000 or version > 7500:
        raise ValueError(f"Неожиданная версия FBX: {version}")

    root_nodes, _ = parse_nodes(fbx_data, 27)

    objects_node = next(n for n in root_nodes if n[0] == "Objects")
    connections_node = next(n for n in root_nodes if n[0] == "Connections")

    objects = objects_node[2]
    connections = connections_node[2]

    geometries = [n for n in objects if n[0] == "Geometry"]
    if not geometries:
        raise ValueError("В FBX не найден Geometry.")

    geometry = geometries[0]

    vertices = np.asarray(
        find_child(geometry, "Vertices")[1][0],
        dtype=np.float32
    ).reshape((-1, 3))

    polygon_vertex_index = np.asarray(
        find_child(geometry, "PolygonVertexIndex")[1][0],
        dtype=np.int64
    )

    normal_layer = find_child(geometry, "LayerElementNormal")
    normals = np.asarray(
        find_child(normal_layer, "Normals")[1][0],
        dtype=np.float32
    ).reshape((-1, 3))
    normal_indices = np.asarray(
        find_child(normal_layer, "NormalsIndex")[1][0],
        dtype=np.int64
    )

    uv_layer = find_child(geometry, "LayerElementUV")
    uvs = np.asarray(
        find_child(uv_layer, "UV")[1][0],
        dtype=np.float32
    ).reshape((-1, 2))
    uv_indices = np.asarray(
        find_child(uv_layer, "UVIndex")[1][0],
        dtype=np.int64
    )

    # Split FBX polygon vertex stream.
    polygons = []
    current = []

    for value in polygon_vertex_index:
        value = int(value)
        if value < 0:
            current.append(-value - 1)
            polygons.append(current)
            current = []
        else:
            current.append(value)

    if current:
        polygons.append(current)

    if log:
        log(
            f"Geometry: {len(vertices):,} control points, "
            f"{len(polygons):,} polygons"
        )

    # Bone hierarchy from FBX OO connections.
    models = [n for n in objects if n[0] == "Model"]
    bone_models = [m for m in models if len(m[1]) >= 3 and m[1][2] == "LimbNode"]

    bone_names = [
        m[1][1].split("\x00", 1)[0]
        for m in bone_models
    ]
    bone_id_by_name = {
        m[1][1].split("\x00", 1)[0]: m[1][0]
        for m in bone_models
    }
    bone_ids = set(bone_id_by_name.values())

    parent = {}
    for connection in connections:
        p = connection[1]
        if len(p) >= 3 and p[0] == "OO":
            child_id, parent_id = p[1], p[2]
            if child_id in bone_ids and parent_id in bone_ids:
                child_name = next(
                    n for n, v in bone_id_by_name.items() if v == child_id
                )
                parent_name = next(
                    n for n, v in bone_id_by_name.items() if v == parent_id
                )
                parent[child_name] = parent_name

    bone_index = {
        name: i for i, name in enumerate(bone_names)
    }

    # Skin weights + bind matrices from FBX Clusters.
    clusters = [
        n for n in objects
        if n[0] == "Deformer"
        and len(n[1]) >= 3
        and n[1][2] == "Cluster"
    ]

    weight_map = [[] for _ in range(len(vertices))]
    global_bind = {}

    for cluster in clusters:
        cluster_id = cluster[1][0]
        cluster_name = cluster[1][1].split("\x00", 1)[0]

        target_bone = None
        for connection in connections:
            p = connection[1]
            if (
                len(p) >= 3
                and p[0] == "OO"
                and p[1] == cluster_id
                and p[2] in bone_ids
            ):
                target_bone = next(
                    n for n, v in bone_id_by_name.items()
                    if v == p[2]
                )
                break

        if target_bone is None:
            target_bone = cluster_name

        transform_link = find_child(cluster, "TransformLink")
        global_bind[target_bone] = normalize_fbx_matrix(
            transform_link[1][0]
        )

        child_names = {c[0] for c in cluster[2]}
        if "Indexes" in child_names and "Weights" in child_names:
            indices = np.asarray(
                find_child(cluster, "Indexes")[1][0],
                dtype=np.int64
            )
            weights = np.asarray(
                find_child(cluster, "Weights")[1][0],
                dtype=np.float64
            )

            for index, weight in zip(indices, weights):
                index = int(index)
                if 0 <= index < len(weight_map):
                    weight_map[index].append(
                        (target_bone, float(weight))
                    )

    # Some files can have a cluster missing from the bind list.
    for name in bone_names:
        global_bind.setdefault(name, np.eye(4, dtype=np.float64))

    local_bind = {}
    for name in bone_names:
        if name in parent:
            local_bind[name] = (
                np.linalg.inv(global_bind[parent[name]])
                @ global_bind[name]
            )
        else:
            local_bind[name] = global_bind[name]

    inverse_bind = np.stack(
        [np.linalg.inv(global_bind[name]) for name in bone_names]
    ).astype(np.float32)

    # Expand FBX polygon vertices into render vertices.
    # Normals/UVs are ByPolygonVertex, so a control point may need duplicates.
    vertex_map = {}
    out_positions = []
    out_normals = []
    out_uvs = []
    out_joints = []
    out_weights = []
    out_triangles = []

    def get_influences(control_point):
        values = sorted(
            weight_map[control_point],
            key=lambda item: item[1],
            reverse=True
        )[:4]

        if not values:
            return [(bone_index.get("root", 0), 1.0)]

        total = sum(w for _, w in values)
        if total <= 1e-12:
            return [(bone_index.get("root", 0), 1.0)]

        return [
            (bone_index[name], weight / total)
            for name, weight in values
        ]

    corner_offset = 0

    for polygon in polygons:
        if len(polygon) < 3:
            corner_offset += len(polygon)
            continue

        # Fan triangulation.
        for k in range(1, len(polygon) - 1):
            triangle = []

            for corner in (0, k, k + 1):
                corner_index = corner_offset + corner
                control_point = polygon[corner]

                normal_index = int(normal_indices[corner_index])
                uv_index = int(uv_indices[corner_index])

                key = (
                    control_point,
                    normal_index,
                    uv_index,
                )

                if key not in vertex_map:
                    output_index = len(out_positions)
                    vertex_map[key] = output_index

                    out_positions.append(vertices[control_point])
                    out_normals.append(normals[normal_index])
                    out_uvs.append(uvs[uv_index])

                    joints = [0, 0, 0, 0]
                    weights4 = [0.0, 0.0, 0.0, 0.0]

                    for q, (joint, weight) in enumerate(
                        get_influences(control_point)
                    ):
                        joints[q] = joint
                        weights4[q] = weight

                    out_joints.append(joints)
                    out_weights.append(weights4)

                triangle.append(vertex_map[key])

            out_triangles.append(triangle)

        corner_offset += len(polygon)

    out_positions = np.asarray(out_positions, dtype=np.float32)
    out_normals = np.asarray(out_normals, dtype=np.float32)
    out_uvs = np.asarray(out_uvs, dtype=np.float32)
    out_joints = np.asarray(out_joints, dtype=np.uint16)
    out_weights = np.asarray(out_weights, dtype=np.float32)
    out_triangles = np.asarray(out_triangles, dtype=np.uint32)

    if len(out_positions) == 0 or len(out_triangles) == 0:
        raise ValueError("Не удалось получить геометрию.")

    # Material.
    materials = [n for n in objects if n[0] == "Material"]
    material_name = "DefaultMaterial"

    if materials:
        material_name = (
            materials[0][1][1].split("\x00", 1)[0]
            or material_name
        )

    # Embedded DDS → PNG.
    png_bytes = None
    if dds_data.startswith(b"DDS "):
        try:
            image = Image.open(io.BytesIO(dds_data)).convert("RGBA")
            image_io = io.BytesIO()
            image.save(image_io, "PNG", optimize=False, compress_level=(1 if fast_texture else 6))
            png_bytes = image_io.getvalue()

            if log:
                log(
                    f"Embedded DDS: {image.width}×{image.height} → PNG"
                )
        except Exception as exc:
            if log:
                log(f"Текстура пропущена: {exc}")

    if log:
        log(
            f"Render mesh: {len(out_positions):,} vertices, "
            f"{len(out_triangles):,} triangles"
        )
        log(f"Skeleton: {len(bone_names)} bones")

    return {
        "positions": out_positions,
        "normals": out_normals,
        "uvs": out_uvs,
        "joints": out_joints,
        "weights": out_weights,
        "triangles": out_triangles,
        "bone_names": bone_names,
        "parent": parent,
        "local_bind": local_bind,
        "inverse_bind": inverse_bind,
        "material_name": material_name,
        "texture_png": png_bytes,
    }


# ----------------------------
# GLB writer
# ----------------------------

def pad4(blob):
    while len(blob) % 4:
        blob.append(0)


def write_glb(scene, path):
    positions = scene["positions"]
    normals = scene["normals"]
    uvs = scene["uvs"]
    joints = scene["joints"]
    weights = scene["weights"]
    triangles = scene["triangles"]
    inverse_bind = scene["inverse_bind"]

    gltf = {
        "asset": {
            "version": "2.0",
            "generator": APP_NAME,
        },
        "scene": 0,
        "scenes": [{"nodes": []}],
        "nodes": [],
        "meshes": [{
            "name": "BNMA_Mesh",
            "primitives": [],
        }],
        "skins": [],
        "materials": [{
            "name": scene["material_name"],
            "pbrMetallicRoughness": {
                "baseColorFactor": [0.8, 0.8, 0.8, 1.0],
                "metallicFactor": 0.0,
                "roughnessFactor": 0.75,
            },
            "doubleSided": True,
        }],
        "buffers": [{"byteLength": 0}],
        "bufferViews": [],
        "accessors": [],
    }

    binary = bytearray()

    def add_array(array, component_type, type_name, target=None,
                  min_value=None, max_value=None):
        pad4(binary)

        offset = len(binary)
        raw = array.tobytes(order="C")
        binary.extend(raw)

        pad4(binary)

        view = {
            "buffer": 0,
            "byteOffset": offset,
            "byteLength": len(raw),
        }

        if target is not None:
            view["target"] = target

        view_index = len(gltf["bufferViews"])
        gltf["bufferViews"].append(view)

        accessor = {
            "bufferView": view_index,
            "componentType": component_type,
            "count": int(len(array)),
            "type": type_name,
        }

        if min_value is not None:
            accessor["min"] = min_value
        if max_value is not None:
            accessor["max"] = max_value

        accessor_index = len(gltf["accessors"])
        gltf["accessors"].append(accessor)
        return accessor_index

    pos_accessor = add_array(
        positions, 5126, "VEC3", 34962,
        positions.min(axis=0).astype(float).tolist(),
        positions.max(axis=0).astype(float).tolist(),
    )

    normal_accessor = add_array(
        normals, 5126, "VEC3", 34962
    )

    uv_accessor = add_array(
        uvs, 5126, "VEC2", 34962
    )

    joints_accessor = add_array(
        joints, 5123, "VEC4", 34962
    )

    weights_accessor = add_array(
        weights, 5126, "VEC4", 34962
    )

    index_accessor = add_array(
        triangles.reshape(-1), 5125, "SCALAR", 34963,
        [0], [int(triangles.max())]
    )

    # glTF MAT4 values are column-major.
    ibm = np.stack([
        matrix.T.reshape(-1)
        for matrix in inverse_bind
    ]).astype(np.float32)

    ibm_accessor = add_array(
        ibm, 5126, "MAT4"
    )

    # Skeleton nodes.
    bone_node_indices = {}

    for name in scene["bone_names"]:
        matrix = scene["local_bind"][name]
        quaternion, scale = quaternion_from_matrix(matrix)

        node = {
            "name": name,
            "translation": matrix[:3, 3].astype(float).tolist(),
            "rotation": quaternion.astype(float).tolist(),
            "scale": scale.astype(float).tolist(),
        }

        bone_node_indices[name] = len(gltf["nodes"])
        gltf["nodes"].append(node)

    # Skeleton hierarchy.
    roots = []

    for name in scene["bone_names"]:
        node_index = bone_node_indices[name]

        if name in scene["parent"]:
            parent_name = scene["parent"][name]
            parent_node = bone_node_indices[parent_name]
            gltf["nodes"][parent_node].setdefault(
                "children", []
            ).append(node_index)
        else:
            roots.append(node_index)

    skin_index = len(gltf["skins"])

    gltf["skins"].append({
        "name": "BNMA_Armature",
        "joints": [
            bone_node_indices[name]
            for name in scene["bone_names"]
        ],
        "inverseBindMatrices": ibm_accessor,
        "skeleton": bone_node_indices.get(
            "root", roots[0] if roots else 0
        ),
    })

    mesh_node = len(gltf["nodes"])
    gltf["nodes"].append({
        "name": "BNMA_Mesh",
        "mesh": 0,
        "skin": skin_index,
    })

    gltf["scenes"][0]["nodes"] = roots + [mesh_node]

    gltf["meshes"][0]["primitives"] = [{
        "attributes": {
            "POSITION": pos_accessor,
            "NORMAL": normal_accessor,
            "TEXCOORD_0": uv_accessor,
            "JOINTS_0": joints_accessor,
            "WEIGHTS_0": weights_accessor,
        },
        "indices": index_accessor,
        "material": 0,
        "mode": 4,
    }]

    # Embedded PNG texture.
    png_bytes = scene.get("texture_png")

    if png_bytes:
        pad4(binary)

        image_offset = len(binary)
        binary.extend(png_bytes)
        pad4(binary)

        view_index = len(gltf["bufferViews"])
        gltf["bufferViews"].append({
            "buffer": 0,
            "byteOffset": image_offset,
            "byteLength": len(png_bytes),
        })

        gltf["images"] = [{
            "name": "base_color",
            "bufferView": view_index,
            "mimeType": "image/png",
        }]

        gltf["samplers"] = [{
            "magFilter": 9729,
            "minFilter": 9987,
            "wrapS": 10497,
            "wrapT": 10497,
        }]

        gltf["textures"] = [{
            "sampler": 0,
            "source": 0,
        }]

        gltf["materials"][0]["pbrMetallicRoughness"][
            "baseColorTexture"
        ] = {"index": 0}

    gltf["buffers"][0]["byteLength"] = len(binary)

    json_bytes = json.dumps(
        gltf,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")

    while len(json_bytes) % 4:
        json_bytes += b" "

    pad4(binary)

    total_length = (
        12
        + 8 + len(json_bytes)
        + 8 + len(binary)
    )

    with open(path, "wb") as f:
        f.write(struct.pack(
            "<4sII",
            b"glTF",
            2,
            total_length,
        ))

        f.write(struct.pack(
            "<I4s",
            len(json_bytes),
            b"JSON",
        ))
        f.write(json_bytes)

        f.write(struct.pack(
            "<I4s",
            len(binary),
            b"BIN\x00",
        ))
        f.write(binary)


# ----------------------------
# Conversion
# ----------------------------

def convert_file(input_path, output_path, extract_fbx=False,
                 fbx_output=None, embed_texture=True, fast_texture=True, log=None):
    with open(input_path, "rb") as f:
        data = f.read()

    if log:
        log(f"Файл: {input_path}")
        log(f"Размер: {len(data):,} байт")
        log("Распаковка BNMA...")

    fbx_data, dds_data, model_offset = decrypt_bnma(data)
    del data

    if log:
        log(
            f"Встроенный FBX найден: offset 0x{model_offset:X}, "
            f"{len(fbx_data):,} байт"
        )
        log(
            f"Embedded DDS: {len(dds_data):,} байт"
            if dds_data else
            "Embedded DDS: отсутствует"
        )

    if extract_fbx:
        if not fbx_output:
            fbx_output = os.path.splitext(output_path)[0] + "_decrypted.fbx"
        with open(fbx_output, "wb") as f:
            f.write(fbx_data)

        if log:
            log(f"Decrypted FBX: {fbx_output}")

    scene = extract_scene(
        fbx_data,
        dds_data,
        log=log,
        fast_texture=fast_texture,
    )

    if not embed_texture:
        scene["texture_png"] = None

    write_glb(scene, output_path)

    if log:
        log(f"GLB готов: {output_path}")

    return {
        "vertices": len(scene["positions"]),
        "triangles": len(scene["triangles"]),
        "bones": len(scene["bone_names"]),
        "texture": bool(scene.get("texture_png")),
    }


