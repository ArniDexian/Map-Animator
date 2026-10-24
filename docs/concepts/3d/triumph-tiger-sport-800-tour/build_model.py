#!/usr/bin/env python3
"""Build a coarse, scan-like glTF model of the concept motorcycle.

Uses only Python's standard library. Coordinates are metres, Y is up, and the
motorcycle faces +Z. Meshes are intentionally approximate: the model is meant
to provide usable volume and silhouette for arbitrary camera angles.
"""

from __future__ import annotations

import json
import math
import os
import struct
import zlib


HERE = os.path.dirname(os.path.abspath(__file__))
GLB_PATH = os.path.join(HERE, "triumph-tiger-sport-800-tour.glb")
PREVIEW_PATH = os.path.join(HERE, "canonical-views.png")
TRANSPARENT_PREVIEW_PATH = os.path.join(HERE, "canonical-views-transparent.png")
LOW_ANGLE_PREVIEW_PATH = os.path.join(HERE, "canonical-views-low-angle.png")


def unit(v):
    length = math.sqrt(sum(x * x for x in v)) or 1.0
    return tuple(x / length for x in v)


def sub(a, b):
    return tuple(x - y for x, y in zip(a, b))


def cross(a, b):
    return (a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0])


class Mesh:
    def __init__(self):
        self.p = []
        self.n = []
        self.i = []

    def append(self, p, n, indices):
        offset = len(self.p)
        self.p.extend(p)
        self.n.extend(n)
        self.i.extend(offset + x for x in indices)


def sphere(center, radii, slices=12, stacks=8):
    m = Mesh()
    p, n, ix = [], [], []
    for iy in range(stacks + 1):
        phi = math.pi * iy / stacks
        sy, sp = math.cos(phi), math.sin(phi)
        for j in range(slices + 1):
            theta = math.tau * j / slices
            x, y, z = sp * math.cos(theta), sy, sp * math.sin(theta)
            p.append((center[0] + x * radii[0], center[1] + y * radii[1], center[2] + z * radii[2]))
            n.append(unit((x / radii[0], y / radii[1], z / radii[2])))
    for iy in range(stacks):
        for j in range(slices):
            a = iy * (slices + 1) + j
            b, c, d = a + 1, a + slices + 1, a + slices + 2
            ix.extend((a, c, b, b, c, d))
    m.append(p, n, ix)
    return m


def cylinder(a, b, radius, sides=10, radius_b=None):
    """Capped low-poly cylinder or tapered capsule segment."""
    m = Mesh()
    axis = unit(sub(b, a))
    u = unit(cross(axis, (0, 1, 0)) if abs(axis[1]) < .92 else cross(axis, (1, 0, 0)))
    v = unit(cross(axis, u))
    rb = radius if radius_b is None else radius_b
    p, n, ix = [], [], []
    for center, rad in ((a, radius), (b, rb)):
        for j in range(sides):
            t = math.tau * j / sides
            outward = unit(tuple(u[k] * math.cos(t) + v[k] * math.sin(t) for k in range(3)))
            p.append(tuple(center[k] + outward[k] * rad for k in range(3)))
            n.append(outward)
    for j in range(sides):
        jn = (j + 1) % sides
        ix.extend((j, sides + j, jn, jn, sides + j, sides + jn))
    # End caps are separate flat faces.
    for cap_center, rad, normal, ring_offset, reverse in ((a, radius, tuple(-x for x in axis), 0, True),
                                                            (b, rb, axis, sides, False)):
        center_index = len(p)
        p.append(cap_center)
        n.append(normal)
        for j in range(sides):
            jn = (j + 1) % sides
            if reverse:
                ix.extend((center_index, ring_offset + jn, ring_offset + j))
            else:
                ix.extend((center_index, ring_offset + j, ring_offset + jn))
    m.append(p, n, ix)
    return m


def helix(a, b, radius, turns=8, tube=.005, steps=96):
    """A small coil spring wrapped around the segment from a to b."""
    axis = unit(sub(b, a))
    u = unit(cross(axis, (1, 0, 0)) if abs(axis[0]) < .92 else cross(axis, (0, 0, 1)))
    v = unit(cross(axis, u))
    centers = []
    for j in range(steps + 1):
        t = j / steps
        angle = math.tau * turns * t
        centers.append(tuple(a[k] + (b[k] - a[k]) * t + radius * (u[k] * math.cos(angle) + v[k] * math.sin(angle)) for k in range(3)))
    m = Mesh()
    for p0, p1 in zip(centers, centers[1:]):
        m.append(*tuple_mesh(cylinder(p0, p1, tube, 6)))
    return m


def torus_wheel(center, major, tube, segments=24, rings=8):
    """Wheel torus with its axis along X; wheel plane is YZ."""
    m = Mesh()
    p, n, ix = [], [], []
    for j in range(segments + 1):
        t = math.tau * j / segments
        radial = (0, math.cos(t), math.sin(t))
        for k in range(rings + 1):
            q = math.tau * k / rings
            nn = (math.cos(q), math.sin(q) * radial[1], math.sin(q) * radial[2])
            p.append((center[0] + tube * math.cos(q),
                      center[1] + (major + tube * math.sin(q)) * radial[1],
                      center[2] + (major + tube * math.sin(q)) * radial[2]))
            n.append(nn)
    for j in range(segments):
        for k in range(rings):
            a = j * (rings + 1) + k
            b, c, d = a + 1, a + rings + 1, a + rings + 2
            ix.extend((a, c, b, b, c, d))
    m.append(p, n, ix)
    return m


def brake_rotor(center, outer=.196, inner=.128, thickness=.012, segments=32):
    """Thin annular brake disc with both faces and an outer edge."""
    m = Mesh()
    p, n, ix = [], [], []
    for x, radial, normal in ((center[0] - thickness / 2, outer, (-1, 0, 0)),
                              (center[0] + thickness / 2, outer, (1, 0, 0)),
                              (center[0] - thickness / 2, inner, (-1, 0, 0)),
                              (center[0] + thickness / 2, inner, (1, 0, 0))):
        for j in range(segments):
            t = math.tau * j / segments
            p.append((x, center[1] + radial * math.cos(t), center[2] + radial * math.sin(t)))
            n.append(normal)
    # Two annular faces and their outer/inner walls.
    for j in range(segments):
        jn = (j + 1) % segments
        lo, ro, li, ri = j, segments + j, 2 * segments + j, 3 * segments + j
        lon, ron, lin, rin = jn, segments + jn, 2 * segments + jn, 3 * segments + jn
        ix.extend((lo, li, lon, lon, li, lin))
        ix.extend((ro, ron, ri, ron, rin, ri))
        ix.extend((lo, lon, ro, lon, ron, ro))
        ix.extend((li, ri, lin, lin, ri, rin))
    m.append(p, n, ix)
    return m


def wheel_fender(center, x_width=.20, inner=.345, outer=.405, start=-1.12, end=1.12, segments=24):
    """Curved strip over the top of a wheel, in its YZ plane."""
    m = Mesh()
    p, n, ix = [], [], []
    for j in range(segments + 1):
        t = start + (end - start) * j / segments
        radial = (math.cos(t), math.sin(t))
        for x, r in ((-x_width / 2, outer), (x_width / 2, outer),
                     (-x_width / 2, inner), (x_width / 2, inner)):
            p.append((x, center[1] + r * radial[0], center[2] + r * radial[1]))
            n.append((0, radial[0], radial[1]))
    for j in range(segments):
        a = j * 4
        b, c, d = a + 1, a + 4, a + 5
        ix.extend((a, c, b, b, c, d))
        a, b, c, d = a + 2, a + 3, a + 6, a + 7
        ix.extend((a, b, c, b, d, c))
    m.append(p, n, ix)
    return m


def loft_z(stations, segments=16):
    """Loft an elliptical cross-section along the bike's forward axis."""
    m = Mesh()
    p, n, ix = [], [], []
    for z, cy, rx, ry in stations:
        for j in range(segments):
            t = math.tau * j / segments
            x, y = math.cos(t), math.sin(t)
            p.append((rx * x, cy + ry * y, z))
            n.append(unit((x / rx, y / ry, 0)))
    for i in range(len(stations) - 1):
        for j in range(segments):
            a = i * segments + j
            b, c, d = i * segments + (j + 1) % segments, (i + 1) * segments + j, (i + 1) * segments + (j + 1) % segments
            ix.extend((a, c, b, b, c, d))
    for ring, z, direction in ((0, stations[0][0], -1), (len(stations) - 1, stations[-1][0], 1)):
        center_index = len(p)
        p.append((0, stations[ring][1], z))
        n.append((0, 0, direction))
        for j in range(segments):
            a, b = ring * segments + j, ring * segments + (j + 1) % segments
            ix.extend((center_index, a, b) if direction < 0 else (center_index, b, a))
    m.append(p, n, ix)
    return m


def loft_y(stations, segments=16):
    """Loft a softly faceted torso section upward along the rider."""
    m = Mesh()
    p, n, ix = [], [], []
    for y, cz, rx, rz in stations:
        for j in range(segments):
            t = math.tau * j / segments
            x, z = math.cos(t), math.sin(t)
            p.append((rx * x, y, cz + rz * z))
            n.append(unit((x / rx, 0, z / rz)))
    for i in range(len(stations) - 1):
        for j in range(segments):
            a = i * segments + j
            b, c, d = i * segments + (j + 1) % segments, (i + 1) * segments + j, (i + 1) * segments + (j + 1) % segments
            ix.extend((a, b, c, b, d, c))
    for ring, direction in ((0, -1), (len(stations) - 1, 1)):
        center_index = len(p)
        p.append((0, stations[ring][0], stations[ring][1]))
        n.append((0, direction, 0))
        for j in range(segments):
            a, b = ring * segments + j, ring * segments + (j + 1) % segments
            ix.extend((center_index, b, a) if direction < 0 else (center_index, a, b))
    m.append(p, n, ix)
    return m


def side_panels(outline, xabs=.30, depth=.035):
    """Create matching solid left/right side plates from (y,z) outlines."""
    m = Mesh()
    count = len(outline)
    for side in (-1, 1):
        x0, x1 = side * (xabs - depth / 2), side * (xabs + depth / 2)
        outer_normal = (float(side), 0, 0)
        base = len(m.p)
        for x, normal in ((x1, outer_normal), (x0, tuple(-a for a in outer_normal))):
            for y, z in outline:
                m.p.append((x, y, z))
                m.n.append(normal)
        for j in range(1, count - 1):
            a, b, c = base, base + j, base + j + 1
            m.i.extend((a, b, c) if side > 0 else (a, c, b))
            a2, b2, c2 = base + count, base + count + j, base + count + j + 1
            m.i.extend((a2, c2, b2) if side > 0 else (a2, b2, c2))
        for j in range(count):
            jn = (j + 1) % count
            a, b, c, d = base + j, base + jn, base + count + j, base + count + jn
            m.i.extend((a, c, b, b, c, d))
    return m


def rounded_box(center, size, bevel=.025, divisions=3):
    """A low-resolution bevel box, useful for luggage and body panels."""
    m = Mesh()
    hx, hy, hz = (x / 2 for x in size)
    half = (hx, hy, hz)
    core = tuple(max(0, h - bevel) for h in half)
    p, n, ix = [], [], []
    face_defs = [
        (0, 1, 2, 1), (0, 1, 2, -1),
        (1, 0, 2, 1), (1, 0, 2, -1),
        (2, 0, 1, 1), (2, 0, 1, -1),
    ]
    for fixed_axis, u_axis, v_axis, sign in face_defs:
        base = len(p)
        for iy in range(divisions + 1):
            for ix0 in range(divisions + 1):
                raw = [0.0, 0.0, 0.0]
                raw[fixed_axis] = sign * half[fixed_axis]
                raw[u_axis] = -half[u_axis] + 2 * half[u_axis] * ix0 / divisions
                raw[v_axis] = -half[v_axis] + 2 * half[v_axis] * iy / divisions
                delta = tuple(raw[k] - max(-core[k], min(core[k], raw[k])) for k in range(3))
                nn = unit(delta)
                pos = tuple(center[k] + max(-core[k], min(core[k], raw[k])) + nn[k] * bevel for k in range(3))
                p.append(pos)
                n.append(nn)
        for iy in range(divisions):
            for ix0 in range(divisions):
                a = base + iy * (divisions + 1) + ix0
                b, c, d = a + 1, a + divisions + 1, a + divisions + 2
                if sign > 0:
                    ix.extend((a, b, c, b, d, c))
                else:
                    ix.extend((a, c, b, b, c, d))
    m.append(p, n, ix)
    return m


def surface(points, double_sided=True):
    m = Mesh()
    p = list(points)
    n = []
    for tri in range(0, len(points), 3):
        a, b, c = points[tri:tri + 3]
        nn = unit(cross(sub(b, a), sub(c, a)))
        n.extend((nn, nn, nn))
    m.append(p, n, list(range(len(p))))
    return m


class Scene:
    def __init__(self):
        self.materials = []
        self.mat_id = {}
        self.parts = []
        self.groups = [
            {"name": "Tiger Sport 800 Tour | Y up, forward +Z", "children": [1, 2]},
            {"name": "Motorcycle", "children": []},
            {"name": "Rider", "children": []},
        ]

    def material(self, name, color, roughness=.72, metallic=0.0, alpha=1.0, emissive=None):
        self.mat_id[name] = len(self.materials)
        mat = {
            "name": name,
            "pbrMetallicRoughness": {
                "baseColorFactor": [*color, alpha],
                "metallicFactor": metallic,
                "roughnessFactor": roughness,
            },
            "doubleSided": True,
        }
        if alpha < 1:
            mat["alphaMode"] = "BLEND"
        if emissive:
            mat["emissiveFactor"] = emissive
        self.materials.append(mat)

    def add(self, name, group, material, mesh):
        node_index = len(self.groups) + len(self.parts)
        mesh_index = len(self.parts)
        self.parts.append({"name": name, "group": group, "material": self.mat_id[material], "mesh": mesh})
        self.groups[group]["children"].append(node_index)


def build_scene():
    s = Scene()
    s.material("Tiger yellow", (.97, .72, .06), .48)
    s.material("Black bodywork", (.045, .052, .062), .7)
    s.material("Suit black", (.065, .07, .08), .88)
    s.material("Tire rubber", (.018, .02, .024), .94)
    s.material("Graphite", (.12, .14, .16), .72, .18)
    s.material("Metal", (.48, .52, .55), .38, .72)
    s.material("Fork gold", (.68, .39, .08), .36, .65)
    s.material("Helmet white", (.88, .91, .92), .42)
    s.material("Dark visor", (.012, .019, .027), .25, .05)
    s.material("Screen tint", (.34, .43, .48), .25, .05, .36)
    s.material("Lamp", (.93, .96, .88), .25, .05, 1.0, (.55, .53, .43))
    s.material("Tail red", (.62, .025, .025), .38)
    s.material("Reflective trim", (.42, .46, .48), .5, .12)

    BIKE, RIDER = 1, 2
    # Wheels: matching front/rear dimensions, with coarse rubber, rim, hub and spokes.
    for label, zc in (("Rear", -.73), ("Front", .76)):
        cy = .365
        s.add(label + " tire", BIKE, "Tire rubber", torus_wheel((0, cy, zc), .285, .094, 32, 10))
        s.add(label + " cast alloy rim", BIKE, "Graphite", torus_wheel((0, cy, zc), .225, .021, 40, 10))
        s.add(label + " rim lip", BIKE, "Metal", torus_wheel((0, cy, zc), .245, .006, 40, 6))
        tread = Mesh()
        for j in range(28):
            t = math.tau * j / 28
            y, z = cy + .365 * math.cos(t), zc + .365 * math.sin(t)
            tread.append(*tuple_mesh(cylinder((-.076, y, z), (.076, y, z), .006, 4)))
        s.add(label + " tread ridges", BIKE, "Graphite", tread)
        s.add(label + " hub", BIKE, "Graphite", cylinder((-.105, cy, zc), (.105, cy, zc), .071, 12))
        s.add(label + " hub cap", BIKE, "Metal", cylinder((-.12, cy, zc), (.12, cy, zc), .038, 12))
        spoke = Mesh()
        for side in (-1, 1):
            for j in range(5):
                t = math.tau * j / 5
                # Five broad, slightly swept cast spokes on each wheel face.
                end = (side * .066, cy + .213 * math.cos(t), zc + .213 * math.sin(t))
                start = (side * .055, cy + .060 * math.cos(t + .27), zc + .060 * math.sin(t + .27))
                spoke.append(*tuple_mesh(cylinder(start, end, .014, 8)))
        s.add(label + " cast spokes", BIKE, "Metal", spoke)
        for side in (-1, 1):
            s.add(label + " brake rotor " + str(side), BIKE, "Metal",
                  brake_rotor((side * .084, cy, zc), .196 if label == "Front" else .17,
                              .128 if label == "Front" else .11, .012, 32))
            if label == "Front":
                s.add("Front brake caliper " + str(side), BIKE, "Graphite",
                      rounded_box((side * .135, .50, zc - .105), (.065, .14, .12), .022, 3))
        if label == "Front":
            s.add("Front wheel arch guard", BIKE, "Black bodywork", wheel_fender((0, cy, zc), .24, .345, .405))

    # Core frame and the angular body volumes that establish the Tiger silhouette.
    frame = Mesh()
    for side in (-1, 1):
        x = side * .19
        frame.append(*tuple_mesh(cylinder((x, .62, -.28), (x, .99, .40), .027, 8)))
        frame.append(*tuple_mesh(cylinder((x, .62, -.28), (x, .49, -.70), .026, 8)))
        frame.append(*tuple_mesh(cylinder((x, .52, -.70), (x, .95, -.53), .022, 8)))
        frame.append(*tuple_mesh(cylinder((x, .80, -.18), (x, 1.12, .40), .021, 8)))
    s.add("Exposed frame tubes", BIKE, "Graphite", frame)

    s.add("Engine and lower chassis mass", BIKE, "Graphite", rounded_box((0, .65, -.01), (.52, .39, .50), .07, 4))
    for side in (-1, 1):
        s.add("Engine side cover " + str(side), BIKE, "Metal",
              cylinder((side * .257, .65, .02), (side * .29, .65, .02), .148, 14))
        s.add("Engine dark inset " + str(side), BIKE, "Black bodywork",
              cylinder((side * .29, .65, .02), (side * .302, .65, .02), .097, 12))
    fins = Mesh()
    for y in (.70, .75, .80):
        for side in (-1, 1):
            fins.append(*tuple_mesh(cylinder((side * .24, y, .03), (side * .27, y, .03), .13, 12)))
    s.add("Engine cylinder fins", BIKE, "Metal", fins)
    s.add("Radiator", BIKE, "Black bodywork", rounded_box((0, .79, .42), (.43, .23, .11), .025, 3))
    grille = Mesh()
    for y in (.70, .735, .77, .805, .84, .875):
        grille.append(*tuple_mesh(cylinder((-.19, y, .482), (.19, y, .482), .006, 5)))
    s.add("Radiator grille", BIKE, "Metal", grille)
    headers = Mesh()
    for x in (-.12, 0, .12):
        points = [(x, .84, .27), (x, .70, .34), (x, .48, .33), (x, .42, .18), (x, .43, -.10)]
        for a, b in zip(points, points[1:]):
            headers.append(*tuple_mesh(cylinder(a, b, .018, 7)))
    s.add("Triple exhaust headers", BIKE, "Fork gold", headers)

    # Swingarm and front fork keep both wheels on one coherent centerline.
    swing = Mesh()
    for side in (-1, 1):
        x = side * .13
        swing.append(*tuple_mesh(cylinder((x, .62, -.24), (x, .365, -.73), .035, 8)))
        swing.append(*tuple_mesh(cylinder((x, .49, -.57), (x, .365, -.73), .026, 8)))
        fork_x = side * .105
        swing.append(*tuple_mesh(cylinder((fork_x, .39, .75), (fork_x, 1.16, .37), .034, 9)))
        swing.append(*tuple_mesh(cylinder((fork_x * .83, .66, .59), (fork_x * .83, 1.20, .35), .022, 9)))
    s.add("Forks and swingarms", BIKE, "Fork gold", swing)
    # Dark fork guards make the lower front section read clearly from any side.
    guards = Mesh()
    for side in (-1, 1):
        x = side * .108
        guards.append(*tuple_mesh(cylinder((x, .39, .75), (x, .72, .57), .047, 8)))
    s.add("Fork lower guards", BIKE, "Black bodywork", guards)
    springs = Mesh()
    pegs = Mesh()
    controls = Mesh()
    for side in (-1, 1):
        springs.append(*tuple_mesh(helix((side * .105, .72, .58), (side * .105, 1.00, .43), .044, 9, .0045, 96)))
        # Rider and passenger footrests are transverse rubber-topped pegs.
        pegs.append(*tuple_mesh(cylinder((side * .18, .59, .04), (side * .39, .59, .04), .026, 9)))
        pegs.append(*tuple_mesh(rounded_box((side * .36, .585, .04), (.10, .045, .075), .016, 2)))
        pegs.append(*tuple_mesh(cylinder((side * .22, .75, -.43), (side * .39, .75, -.43), .020, 8)))
        # Brake and shift levers sit immediately ahead of the rider's boots.
        controls.append(*tuple_mesh(cylinder((side * .20, .56, .10), (side * .32, .55, .22), .010, 6)))
        controls.append(*tuple_mesh(cylinder((side * .32, .55, .22), (side * .32, .55, .28), .012, 6)))
    s.add("Fork coil springs", BIKE, "Metal", springs)
    s.add("Rider and passenger foot pegs", BIKE, "Graphite", pegs)
    s.add("Foot controls", BIKE, "Metal", controls)

    # Fuel tank, separate protector pad and filler cap.
    s.add("Fuel tank", BIKE, "Tiger yellow", loft_z([
        (-.27, 1.03, .21, .12), (-.15, 1.06, .275, .19),
        (.04, 1.08, .305, .225), (.24, 1.08, .29, .21),
        (.39, 1.04, .235, .15), (.47, 1.00, .18, .10),
    ], 18))
    for side in (-1, 1):
        s.add("Tank dark flank " + str(side), BIKE, "Black bodywork",
              sphere((side * .245, .99, .18), (.075, .13, .26), 10, 7))
    s.add("Tank protection pad", BIKE, "Black bodywork", sphere((0, 1.257, -.03), (.112, .018, .12), 12, 6))
    s.add("Fuel filler cap", BIKE, "Metal", cylinder((0, 1.258, .25), (0, 1.275, .25), .062, 16))

    # Front fairing and headlamp pods.
    s.add("Main front fairing", BIKE, "Tiger yellow", loft_z([
        (.36, 1.00, .22, .16), (.48, 1.02, .32, .24),
        (.68, 1.07, .365, .28), (.84, 1.11, .335, .26),
        (.98, 1.11, .26, .19), (1.075, 1.085, .17, .12),
    ], 20))
    panels = Mesh()
    # Upper shell flares from the narrow nose to the tank side, like the
    # angular Tiger fairing in the supplied turnaround.
    outline = [(1.24, .74), (1.16, .91), (1.03, 1.02), (.88, .98),
               (.79, .81), (.69, .56), (.78, .43), (.96, .48),
               (1.10, .56), (1.20, .61)]
    panels.append(*tuple_mesh(side_panels(outline, .35, .055)))
    s.add("Angular side fairings", BIKE, "Tiger yellow", panels)
    # Rounded side shoulders fill the space between the central nose loft and
    # the angular outer panels, so the fairing reads as a formed shell rather
    # than a pair of thin plates when the model is rotated.
    for side in (-1, 1):
        s.add("Fairing shoulder " + str(side), BIKE, "Tiger yellow",
              sphere((side * .235, .845, .78), (.16, .245, .29), 16, 10))
    lowers = Mesh()
    outline = [(1.00, .53), (.87, .64), (.71, .56), (.58, .30),
               (.64, .12), (.79, .18), (.89, .32)]
    lowers.append(*tuple_mesh(side_panels(outline, .325, .045)))
    s.add("Black lower fairings", BIKE, "Black bodywork", lowers)
    vents = Mesh()
    indicators = Mesh()
    for side in (-1, 1):
        vents.append(*tuple_mesh(rounded_box((side * .382, .855, .77), (.025, .105, .17), .018, 3)))
        indicators.append(*tuple_mesh(sphere((side * .37, .91, .96), (.035, .025, .045), 8, 5)))
    s.add("Fairing side vents", BIKE, "Graphite", vents)
    s.add("Front side indicators", BIKE, "Lamp", indicators)
    lamps = Mesh()
    bulbs = Mesh()
    for side in (-1, 1):
        x = side * .13
        lamps.append(*tuple_mesh(sphere((x, 1.065, 1.082), (.105, .05, .038), 12, 7)))
        bulbs.append(*tuple_mesh(sphere((x, 1.07, 1.11), (.076, .029, .018), 12, 7)))
    s.add("Twin headlight surrounds", BIKE, "Black bodywork", lamps)
    s.add("Twin headlights", BIKE, "Lamp", bulbs)

    # Windshield: thin curved strip, tinted but transparent in the GLB.
    windshield = Mesh()
    rows = [(.0, .22, 1.00), (.15, .19, .94), (.27, .13, .82)]
    for y, width, z in rows:
        for k in range(5):
            x = -width + 2 * width * k / 4
            bulge = .025 * (1 - (x / width) ** 2)
            windshield.p.append((x, 1.24 + y, z + bulge))
            windshield.n.append((0, .55, .83))
    for r in range(2):
        for k in range(4):
            a = r * 5 + k
            b, c, d = a + 1, a + 5, a + 6
            windshield.i.extend((a, c, b, b, c, d))
    s.add("Tinted windscreen", BIKE, "Screen tint", windshield)

    # Seat, tail and rear luggage carrier.
    s.add("Rider seat", BIKE, "Black bodywork", sphere((0, 1.005, -.28), (.27, .105, .43), 14, 8))
    s.add("Rear tail body", BIKE, "Tiger yellow", loft_z([
        (-.83, .98, .22, .105), (-.70, 1.00, .27, .15),
        (-.55, 1.02, .25, .14), (-.43, 1.02, .19, .10),
    ], 14))
    s.add("Rear light", BIKE, "Tail red", rounded_box((0, .85, -.86), (.24, .085, .06), .02, 2))
    rack = Mesh()
    for side in (-1, 1):
        rack.append(*tuple_mesh(cylinder((side * .12, 1.03, -.47), (side * .18, 1.24, -.91), .018, 7)))
    s.add("Luggage rack", BIKE, "Graphite", rack)

    # Hard panniers: shaped black shells with tapered color panels and insets.
    pannier_racks = Mesh()
    for side in (-1, 1):
        x = side
        rack_path = [((x * .20, .94, -.39), (x * .30, .91, -.42)),
                     ((x * .30, .91, -.42), (x * .30, .76, -.87)),
                     ((x * .30, .76, -.87), (x * .55, .76, -.87)),
                     ((x * .55, .76, -.87), (x * .55, .94, -.47)),
                     ((x * .55, .94, -.47), (x * .30, .91, -.42))]
        for a, b in rack_path:
            pannier_racks.append(*tuple_mesh(cylinder(a, b, .014, 7)))
    s.add("Pannier mounting frames", BIKE, "Graphite", pannier_racks)
    pannier_yellow = [(.96, -.84), (.91, -.50), (.84, -.41), (.76, -.45),
                      (.70, -.70), (.74, -.87), (.84, -.91)]
    pannier_inset = [(.82, -.82), (.85, -.54), (.79, -.49), (.75, -.56),
                     (.75, -.78), (.79, -.85)]
    for side in (-1, 1):
        sx = side * .51
        s.add("Pannier shell " + str(side), BIKE, "Black bodywork",
              rounded_box((sx, .83, -.65), (.39, .34, .50), .055, 3))
        s.add("Pannier yellow face " + str(side), BIKE, "Tiger yellow",
              side_panels(pannier_yellow, .713, .014))
        s.add("Pannier gray inset " + str(side), BIKE, "Graphite",
              side_panels(pannier_inset, .724, .008))
        s.add("Pannier rear reflector " + str(side), BIKE, "Tail red",
              rounded_box((side * .52, .92, -.908), (.18, .032, .018), .008, 2))

    # Top case and backrest; its upper surface is left plain.
    s.add("Top case shell", BIKE, "Black bodywork", rounded_box((0, 1.34, -.99), (.58, .30, .40), .06, 3))
    s.add("Top case yellow lid", BIKE, "Tiger yellow", rounded_box((0, 1.485, -.99), (.48, .055, .34), .025, 3))
    s.add("Top case passenger backrest", BIKE, "Suit black", rounded_box((0, 1.335, -.765), (.32, .16, .045), .025, 3))
    s.add("Top case rear reflector", BIKE, "Tail red", rounded_box((0, 1.30, -1.194), (.30, .045, .014), .012, 2))

    # Right-side exhaust, simple but clearly separated from the pannier.
    s.add("Right side exhaust can", BIKE, "Metal", cylinder((.30, .56, -.30), (.34, .57, -.92), .085, 12, .068))
    s.add("Exhaust tip", BIKE, "Graphite", cylinder((.34, .57, -.92), (.34, .57, -.955), .054, 12))
    s.add("Exhaust heat shield", BIKE, "Graphite", rounded_box((.30, .61, -.63), (.035, .10, .36), .02, 2))
    s.add("Exhaust collector", BIKE, "Metal", cylinder((.02, .43, -.10), (.27, .51, -.34), .045, 9, .06))

    # Rear drive hardware on the left side helps distinguish the touring bike
    # from a bicycle in the side view without adding tiny decorative parts.
    s.add("Rear sprocket", BIKE, "Metal", torus_wheel((-.157, .365, -.73), .132, .012, 20, 5))
    chain = Mesh()
    chain_path = [(-.16, .48, -.65), (-.16, .59, -.43), (-.16, .70, -.12),
                  (-.16, .67, .08), (-.16, .57, -.03), (-.16, .47, -.28),
                  (-.16, .40, -.55), (-.16, .29, -.78), (-.16, .27, -.90),
                  (-.16, .40, -.85)]
    for a, b in zip(chain_path, chain_path[1:] + chain_path[:1]):
        chain.append(*tuple_mesh(cylinder(a, b, .009, 5)))
    s.add("Drive chain", BIKE, "Graphite", chain)

    # Mirrors and handlebars.
    s.add("Handlebar", BIKE, "Graphite", cylinder((-.35, 1.195, .40), (.35, 1.195, .40), .018, 10))
    mirrors = Mesh()
    for side in (-1, 1):
        mirrors.append(*tuple_mesh(cylinder((side * .25, 1.20, .40), (side * .39, 1.34, .48), .012, 7)))
        mirrors.append(*tuple_mesh(sphere((side * .43, 1.35, .49), (.105, .038, .072), 10, 6)))
    s.add("Mirrors", BIKE, "Black bodywork", mirrors)

    # Rider jacket, neck, helmet and forward-reaching arms.
    s.add("Rider torso", RIDER, "Suit black", loft_y([
        (1.24, -.12, .20, .16), (1.32, -.08, .235, .18),
        (1.46, .00, .205, .175), (1.60, .11, .23, .17),
        (1.68, .13, .19, .145),
    ], 16))
    s.add("Rider hips", RIDER, "Suit black", sphere((0, 1.255, -.12), (.215, .14, .19), 12, 8))
    s.add("Helmet neck", RIDER, "Suit black", cylinder((0, 1.68, .13), (0, 1.78, .15), .075, 10))
    s.add("White full-face helmet", RIDER, "Helmet white", loft_z([
        (-.025, 1.875, .085, .10), (.015, 1.89, .135, .145),
        (.09, 1.89, .155, .17), (.17, 1.87, .15, .155),
        (.24, 1.83, .125, .12), (.295, 1.80, .075, .075),
        (.315, 1.79, .025, .035),
    ], 20))
    s.add("Helmet dark visor", RIDER, "Dark visor", sphere((0, 1.905, .252), (.132, .067, .046), 16, 10))
    s.add("Helmet chin section", RIDER, "Helmet white", rounded_box((0, 1.785, .278), (.205, .082, .083), .03, 4))

    arms = Mesh()
    legs = Mesh()
    boots = Mesh()
    trim = Mesh()
    for side in (-1, 1):
        shoulder = (side * .205, 1.64, .16)
        elbow = (side * .285, 1.47, .34)
        wrist = (side * .315, 1.205, .40)
        arms.append(*tuple_mesh(cylinder(shoulder, elbow, .087, 10, .071)))
        arms.append(*tuple_mesh(cylinder(elbow, wrist, .071, 10, .054)))
        arms.append(*tuple_mesh(sphere(wrist, (.065, .055, .07), 8, 6)))
        # Bright reflective bands, subtle and easy to read at small sizes.
        trim.append(*tuple_mesh(cylinder((side * .284, 1.49, .31), (side * .292, 1.47, .33), .075, 8, .072)))
        hip = (side * .16, 1.27, -.12)
        knee = (side * .24, 1.02, .12)
        ankle = (side * .25, .66, -.07)
        legs.append(*tuple_mesh(cylinder(hip, knee, .115, 10, .09)))
        legs.append(*tuple_mesh(cylinder(knee, ankle, .083, 10, .07)))
        boots.append(*tuple_mesh(sphere((side * .25, .61, .04), (.09, .085, .19), 9, 6)))
    s.add("Rider arms and gloves", RIDER, "Suit black", arms)
    s.add("Rider trouser legs", RIDER, "Suit black", legs)
    s.add("Rider touring boots", RIDER, "Black bodywork", boots)
    s.add("Suit reflective details", RIDER, "Reflective trim", trim)
    return s


def densify_scene(scene, scan_relief=.0012):
    """Subdivide the blockout into a dense, lightly irregular scan-like mesh."""
    def noise(x, y, z):
        raw = math.sin(x * 127.1 + y * 311.7 + z * 74.7) * 43758.5453
        return (raw - math.floor(raw)) * 2 - 1

    for part in scene.parts:
        mesh = part["mesh"]
        positions = list(mesh.p)
        normals = list(mesh.n)
        midpoint_cache = {}

        def midpoint(a, b):
            key = (min(a, b), max(a, b))
            if key in midpoint_cache:
                return midpoint_cache[key]
            pa, pb = positions[a], positions[b]
            na, nb = normals[a], normals[b]
            pos = tuple((pa[k] + pb[k]) * .5 for k in range(3))
            normal = unit(tuple(na[k] + nb[k] for k in range(3)))
            displacement = scan_relief * noise(*pos)
            pos = tuple(pos[k] + normal[k] * displacement for k in range(3))
            index = len(positions)
            positions.append(pos)
            normals.append(normal)
            midpoint_cache[key] = index
            return index

        refined = []
        for offset in range(0, len(mesh.i), 3):
            a, b, c = mesh.i[offset:offset + 3]
            ab, bc, ca = midpoint(a, b), midpoint(b, c), midpoint(c, a)
            refined.extend((a, ab, ca, ab, b, bc, ca, bc, c, ab, bc, ca))
        mesh.p, mesh.n, mesh.i = positions, normals, refined


def tuple_mesh(mesh):
    return mesh.p, mesh.n, mesh.i


def build_glb(scene, path):
    binary = bytearray()
    views, accessors, meshes, nodes = [], [], [], []

    def add_view(data, target):
        while len(binary) % 4:
            binary.append(0)
        offset = len(binary)
        binary.extend(data)
        view = {"buffer": 0, "byteOffset": offset, "byteLength": len(data)}
        if target:
            view["target"] = target
        views.append(view)
        return len(views) - 1

    for part in scene.parts:
        mesh = part["mesh"]
        pos_flat = [c for v in mesh.p for c in v]
        nor_flat = [c for v in mesh.n for c in v]
        pos_data = struct.pack("<%sf" % len(pos_flat), *pos_flat)
        nor_data = struct.pack("<%sf" % len(nor_flat), *nor_flat)
        index_fmt = "H" if len(mesh.p) < 65536 else "I"
        component_type = 5123 if index_fmt == "H" else 5125
        idx_data = struct.pack("<%s%s" % (len(mesh.i), index_fmt), *mesh.i)
        pv, nv, iv = add_view(pos_data, 34962), add_view(nor_data, 34962), add_view(idx_data, 34963)
        mins = [min(v[k] for v in mesh.p) for k in range(3)]
        maxs = [max(v[k] for v in mesh.p) for k in range(3)]
        pa = len(accessors)
        accessors.append({"bufferView": pv, "componentType": 5126, "count": len(mesh.p), "type": "VEC3", "min": mins, "max": maxs})
        na = len(accessors)
        accessors.append({"bufferView": nv, "componentType": 5126, "count": len(mesh.n), "type": "VEC3"})
        ia = len(accessors)
        accessors.append({"bufferView": iv, "componentType": component_type, "count": len(mesh.i), "type": "SCALAR"})
        meshes.append({"name": part["name"], "primitives": [{"attributes": {"POSITION": pa, "NORMAL": na}, "indices": ia, "material": part["material"], "mode": 4}]})
        group_node = part["group"]
        nodes.append({"name": part["name"], "mesh": len(meshes) - 1})
        scene.groups[group_node]["children"].append(len(scene.groups) + len(nodes) - 1)

    gltf_nodes = scene.groups + nodes
    gltf = {
        "asset": {"version": "2.0", "generator": "coarse-motorcycle-model-builder"},
        "scene": 0,
        "scenes": [{"name": "Tiger Sport 800 Tour | scan-like approximation", "nodes": [0]}],
        "nodes": gltf_nodes,
        "meshes": meshes,
        "materials": scene.materials,
        "accessors": accessors,
        "bufferViews": views,
        "buffers": [{"byteLength": len(binary)}],
    }
    json_bytes = json.dumps(gltf, separators=(",", ":")).encode("utf-8")
    while len(json_bytes) % 4:
        json_bytes += b" "
    while len(binary) % 4:
        binary.append(0)
    total_len = 12 + 8 + len(json_bytes) + 8 + len(binary)
    with open(path, "wb") as f:
        f.write(struct.pack("<4sII", b"glTF", 2, total_len))
        f.write(struct.pack("<I4s", len(json_bytes), b"JSON"))
        f.write(json_bytes)
        f.write(struct.pack("<I4s", len(binary), b"BIN\x00"))
        f.write(binary)


def png_chunk(kind, payload):
    return (struct.pack(">I", len(payload)) + kind + payload +
            struct.pack(">I", zlib.crc32(kind + payload) & 0xffffffff))


def write_png(path, width, height, rgba):
    rows = bytearray()
    stride = width * 4
    for y in range(height):
        rows.append(0)
        rows.extend(rgba[y * stride:(y + 1) * stride])
    data = b"\x89PNG\r\n\x1a\n"
    data += png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
    data += png_chunk(b"IDAT", zlib.compress(bytes(rows), 7))
    data += png_chunk(b"IEND", b"")
    with open(path, "wb") as f:
        f.write(data)


def render_preview(scene, path, transparent=False, elevation_degrees=60):
    """2x2 orthographic preview from the four cardinal axes at 60° elevation."""
    cell, width, height = 640, 1280, 1280
    canvas = bytearray(width * height * 4)
    light = unit((-.38, .82, .42))
    elevation = math.radians(elevation_degrees)
    ce, se = math.cos(elevation), math.sin(elevation)
    up_world = (0, 1, 0)
    # Camera outward vectors: rear, right side, left side, front.
    outward = [(0, se, -ce), (ce, se, 0), (-ce, se, 0), (0, se, ce)]
    # Horizontal screen direction is chosen so the side views face outward.
    # The up vector is the camera's elevated vertical axis.
    cameras = []
    for v in outward:
        right = unit(cross(v, up_world))
        camera_up = unit(cross(right, v))
        cameras.append((right, camera_up, v))
    views = [
        ("rear", 0, 0, cameras[0]),
        ("right side", 1, 0, cameras[1]),
        ("left side", 0, 1, cameras[2]),
        ("front", 1, 1, cameras[3]),
    ]
    target = (0, .95, 0)
    extents = []
    for _, _, _, (right, camera_up, view_dir) in views:
        projected = [(sum((p[k] - target[k]) * right[k] for k in range(3)),
                      sum((p[k] - target[k]) * camera_up[k] for k in range(3)))
                     for part in scene.parts for p in part["mesh"].p]
        minx, maxx = min(p[0] for p in projected), max(p[0] for p in projected)
        miny, maxy = min(p[1] for p in projected), max(p[1] for p in projected)
        extents.append((minx, maxx, miny, maxy))
    span = max(max(x1 - x0, y1 - y0) for x0, x1, y0, y1 in extents)
    scale = (cell - 48) / max(span, .01)
    for name, col, row, project in views:
        x0, y0 = col * cell, row * cell
        right, camera_up, view_dir = project
        zbuf = [-1e30] * (cell * cell)
        bg = (0, 0, 0, 0) if transparent else (236, 238, 240, 255)
        rgba = bytearray(bg * (cell * cell))

        def screen(p):
            relative = tuple(p[k] - target[k] for k in range(3))
            return (cell / 2 + sum(relative[k] * right[k] for k in range(3)) * scale,
                    cell / 2 - sum(relative[k] * camera_up[k] for k in range(3)) * scale,
                    sum(relative[k] * view_dir[k] for k in range(3)))

        for part in scene.parts:
            mesh = part["mesh"]
            mat = scene.materials[part["material"]]
            base = mat["pbrMetallicRoughness"]["baseColorFactor"]
            def to_srgb(value):
                value = max(0.0, min(1.0, value))
                return 12.92 * value if value <= .0031308 else 1.055 * value ** (1 / 2.4) - .055
            base_rgb = tuple(max(0, min(255, int(to_srgb(c) * 255))) for c in base[:3])
            alpha = max(0, min(255, int(base[3] * 255)))
            for idx in range(0, len(mesh.i), 3):
                ids = mesh.i[idx:idx + 3]
                pts = [screen(mesh.p[i]) for i in ids]
                ns = [mesh.n[i] for i in ids]
                sx = [p[0] for p in pts]
                sy = [p[1] for p in pts]
                min_px = max(0, int(min(sx)))
                max_px = min(cell - 1, int(max(sx) + 1))
                min_py = max(0, int(min(sy)))
                max_py = min(cell - 1, int(max(sy) + 1))
                denom = ((pts[1][1] - pts[2][1]) * (pts[0][0] - pts[2][0]) +
                         (pts[2][0] - pts[1][0]) * (pts[0][1] - pts[2][1]))
                if abs(denom) < 1e-8:
                    continue
                shades = [max(.26, .50 + .62 * max(0, sum(ns[k][j] * light[j] for j in range(3)))) for k in range(3)]
                for py in range(min_py, max_py + 1):
                    for px in range(min_px, max_px + 1):
                        xx, yy = px + .5, py + .5
                        b0 = ((pts[1][1] - pts[2][1]) * (xx - pts[2][0]) + (pts[2][0] - pts[1][0]) * (yy - pts[2][1])) / denom
                        b1 = ((pts[2][1] - pts[0][1]) * (xx - pts[2][0]) + (pts[0][0] - pts[2][0]) * (yy - pts[2][1])) / denom
                        b2 = 1 - b0 - b1
                        if b0 < -1e-5 or b1 < -1e-5 or b2 < -1e-5:
                            continue
                        depth = b0 * pts[0][2] + b1 * pts[1][2] + b2 * pts[2][2]
                        pix = py * cell + px
                        if depth <= zbuf[pix]:
                            continue
                        zbuf[pix] = depth
                        shade = b0 * shades[0] + b1 * shades[1] + b2 * shades[2]
                        off = pix * 4
                        color = (min(255, int(base_rgb[0] * shade)),
                                 min(255, int(base_rgb[1] * shade)),
                                 min(255, int(base_rgb[2] * shade)))
                        mix = alpha / 255
                        if transparent:
                            if alpha:
                                rgba[off] = color[0]
                                rgba[off + 1] = color[1]
                                rgba[off + 2] = color[2]
                                rgba[off + 3] = alpha
                        else:
                            mix = alpha / 255
                            rgba[off] = int(color[0] * mix + bg[0] * (1 - mix))
                            rgba[off + 1] = int(color[1] * mix + bg[1] * (1 - mix))
                            rgba[off + 2] = int(color[2] * mix + bg[2] * (1 - mix))
                            rgba[off + 3] = 255
        for y in range(cell):
            start = y * cell * 4
            dest = ((y0 + y) * width + x0) * 4
            canvas[dest:dest + cell * 4] = rgba[start:start + cell * 4]
    write_png(path, width, height, canvas)


def main():
    scene = build_scene()
    render_preview(scene, PREVIEW_PATH, transparent=False)
    render_preview(scene, TRANSPARENT_PREVIEW_PATH, transparent=True)
    render_preview(scene, LOW_ANGLE_PREVIEW_PATH, transparent=False, elevation_degrees=25)
    densify_scene(scene)
    triangles = sum(len(part["mesh"].i) // 3 for part in scene.parts)
    build_glb(scene, GLB_PATH)
    print("Wrote", GLB_PATH)
    print("Wrote", PREVIEW_PATH)
    print("Wrote", TRANSPARENT_PREVIEW_PATH)
    print("Wrote", LOW_ANGLE_PREVIEW_PATH)
    print("Meshes:", len(scene.parts))
    print("Triangles:", triangles)


if __name__ == "__main__":
    main()
