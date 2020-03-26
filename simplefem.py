import numpy as np
from scipy.sparse import csc_matrix, coo_matrix
from scipy.sparse.linalg import spsolve
import trimesh
import pyvista as pv
from pyvista import examples
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d import Axes3D  # noqa: F401 unused import
import pandas as pd
import tetgen
from mayavi import mlab


class VertexManager:
    def __init__(self, shape):
        # TODO vertices is fast, but not space efficient, is this a problem?
        self.vertices_index = np.ones(shape) * -1  # we assume -1 has no index assigned to it
        self.current_vertex_index = 0
        self.vertices = []

    def get_or_generate(self, coordinates):
        # expects coordinates like (i, j, k)
        i, j, k = coordinates
        if self.vertices_index[i, j, k] == -1:
            self.vertices_index[i, j, k] = self.current_vertex_index
            self.vertices.append(coordinates)
            self.current_vertex_index += 1
        
        return self.vertices_index[i, j, k]



def generate_elasticity_mat(youngs, poissons):
    E = youngs
    v = poissons
    mat = np.array([[1 - v, v, v, 0, 0, 0],
                    [v, 1 - v, v, 0, 0, 0],
                    [v, v, 1 - v, 0, 0, 0],
                    [0, 0, 0, 1 - 2*v, 0, 0],
                    [0, 0, 0, 0, 1 - 2*v, 0],
                    [0, 0, 0, 0, 0, 1 - 2*v],
                   ])
    elasticity_mat = E / ((1 + v) * (1 - 2 * v)) * mat
    return elasticity_mat


def add_local_stiffness(D, K_g, nodes, el_nodes):
    # https://academic.csuohio.edu/duffy_s/CVE_512_12.pdf
    x = np.array([nodes[el_nodes[0]][0], nodes[el_nodes[1]][0], nodes[el_nodes[2]][0], nodes[el_nodes[3]][0]])
    y = np.array([nodes[el_nodes[0]][1], nodes[el_nodes[1]][1], nodes[el_nodes[2]][1], nodes[el_nodes[3]][1]])
    z = np.array([nodes[el_nodes[0]][2], nodes[el_nodes[1]][2], nodes[el_nodes[2]][2], nodes[el_nodes[3]][2]])
    ones = np.array([1.0, 1.0, 1.0, 1.0])
    
    C = np.vstack((ones.T, x.T, y.T, z.T))
    IC = np.linalg.inv(C)
    B = np.zeros((6, 12))
    
    for i in range(4):
        B[0, i*3] = IC[1, i]
        B[1, i*3 + 1] = IC[2, i]
        B[2, i*3 + 2] = IC[3, i]
        
        B[3, i*3] = IC[2, i]
        B[3, i*3 + 1] = IC[1, i]
        
        B[4, i*3 + 1] = IC[3, i]
        B[4, i*3 + 2] = IC[2, i]
        
        B[5, i*3] = IC[3, i]
        B[5, i*3 + 2] = IC[1, i]
        
    B = B / (np.linalg.det(C))
    
    tet_volume = 1 / 6. * np.abs(np.linalg.det(C))
    K_loc_unscaled = np.dot(np.dot(B.T, D.T), B)
    K_l = K_loc_unscaled * tet_volume

    for i in range(4):
        for j in range(4):
            K_g.append([int(3 * el_nodes[i] + 0), int(3 * el_nodes[j] + 0), K_l[3 * i + 0, 3 * j + 0]])
            K_g.append([int(3 * el_nodes[i] + 0), int(3 * el_nodes[j] + 1), K_l[3 * i + 0, 3 * j + 1]])
            K_g.append([int(3 * el_nodes[i] + 0), int(3 * el_nodes[j] + 2), K_l[3 * i + 0, 3 * j + 2]])

            K_g.append([int(3 * el_nodes[i] + 1), int(3 * el_nodes[j] + 0), K_l[3 * i + 1, 3 * j + 0]])
            K_g.append([int(3 * el_nodes[i] + 1), int(3 * el_nodes[j] + 1), K_l[3 * i + 1, 3 * j + 1]])
            K_g.append([int(3 * el_nodes[i] + 1), int(3 * el_nodes[j] + 2), K_l[3 * i + 1, 3 * j + 2]])

            K_g.append([int(3 * el_nodes[i] + 2), int(3 * el_nodes[j] + 0), K_l[3 * i + 2, 3 * j + 0]])
            K_g.append([int(3 * el_nodes[i] + 2), int(3 * el_nodes[j] + 1), K_l[3 * i + 2, 3 * j + 1]])
            K_g.append([int(3 * el_nodes[i] + 2), int(3 * el_nodes[j] + 2), K_l[3 * i + 2, 3 * j + 2]])
    
    return B

def apply_constraints(K_g_sp, constraints):
    # constraint is of type [(node, [x, y, z]), ...] where xyz are 1 for constraint, 0 for no constraint
    for constraint in constraints:
        for i in range(len(constraint[1])):
            if constraint[1][i] == 1:
                idx = constraint[0]*3 + i
                K_g_sp[idx, :] = 0.0
                K_g_sp[:, idx] = 0.0
        for i in range(len(constraint[1])):
            if constraint[1][i] == 1:
                idx = constraint[0]*3 + i
                K_g_sp[idx, idx] = 1.0
                
def get_loads(loads, verts):
    # loads is of tpye [(node, x, y, z)]
    load_vec = np.zeros(len(verts)*3)
    for load in loads:
        node = load[0]
        load_vec[3*node + 0] = load[1]
        load_vec[3*node + 1] = load[2]
        load_vec[3*node + 2] = load[3]
    return load_vec
        
def solve_fem(K_g_sp, loads):
    d = spsolve(K_g_sp, loads)
    return d

def solve_full(elements, verts, poisson, youngs, constraints, loads):
    D = generate_elasticity_mat(youngs, poisson)
    load_arr = get_loads(loads, verts)
    K_g = []
    for el in elements:
        add_local_stiffness(D, K_g, verts, el)
    data = [k[2] for k in K_g]
    rows = [k[0] for k in K_g]
    cols = [k[1] for k in K_g]
    K_g_sp = coo_matrix((data, (rows, cols))).tolil()
    apply_constraints(K_g_sp, constraints)
    displacements = solve_fem(K_g_sp, load_arr)
    return displacements

def extract_tets(cells):
    cell_arr = []
    for i in range(len(cells) // 5):
        start = i*5 + 1
        cell_arr.append(cells[start:start + 4])
    return np.array(cell_arr)

def get_displacement_magnitudes(displacements):
    num_verts = len(displacements) // 3
    mags = np.linalg.norm(displacements.reshape((num_verts, 3)), axis=1)
    return mags

def generate_unstructured_grid(elem, node):
    buf = np.empty((elem.shape[0], 1), np.int64)
    cell_type = np.empty(elem.shape[0], dtype='uint8')
    if elem.shape[1] == 4:  # linear
        buf[:] = 4
        cell_type[:] = 10
    elif elem.shape[1] == 10:  # quadradic
        buf[:] = 10
        cell_type[:] = 24
    else:
        raise Exception('Invalid element array shape %s' % str(elem.shape))

    offset = np.cumsum(buf + 1) - (buf[0] + 1)
    cells = np.hstack((buf, elem))
    grid = pv.UnstructuredGrid(offset, cells, cell_type, node)
    return grid


def display_tets(verts, elements, magnitudes=None):
    triangles = []
    for el in elements:
        e = el.tolist()
        double_el = e + e
        for i in range(4):
            triangles.append(double_el[i:i+3])
    
    triangles = np.array(triangles)
    
    grid = generate_unstructured_grid(elements, verts)
    grid.plot(scalars=magnitudes, stitle='Quality', cmap='bwr',
             flip_scalars=True, show_edges=True,)

def indices_to_vector(i, j, k, shape):
    val = i*shape[2]*shape[1] + j*shape[2] + k
    print(val, i, j, k, shape)
    return val

def grid_to_tets(grid):
    manager = VertexManager(grid.shape)
    tets = []
    for i in range(grid.shape[0]):
        for j in range(grid.shape[1]):
            for k in range(grid.shape[2]):
                if np.all(grid[i:i+2, j:j+2, k:k+2] > 0):
                    new_tets = get_offset_tet(np.array([i, j, k]), manager)
                    tets += new_tets
    return np.array(tets, dtype=np.int), np.array(manager.vertices)


def get_offset_tet(base_idx, manager):
    # get a base index
    # generate local vertices
    # conver to global vertices
    # generate global nodes
    tets_local = np.array([[5, 7, 1, 2],
                     [6, 4, 3, 0],
                     [7, 0, 1, 6],
                     [5, 7, 0, 1],
                     [6, 4, 0, 1],
                     [1, 0, 5, 4]])
    vertices = np.array([[0,  1, 0],
                         [1, 0,  1],
                         [1,  1,  1],
                         [0, 0, 0],
                         [1, 0, 0],
                         [1,  1, 0],
                         [0, 0,  1],
                         [0,  1,  1]])
    tets = []
    for tet in tets_local:
        single_tet = []
        for node in tet:
            local_vert = vertices[node]
            global_vert = local_vert + base_idx
            global_ind = manager.get_or_generate(global_vert)
            single_tet.append(global_ind)
        tets.append(single_tet)
    return tets


def test_box_grid():
    grid = np.zeros((8, 8, 8))
    grid[3:6, 2:6, 3:6] = 1
    elements, vertices = grid_to_tets(grid)
    print(elements, vertices)
    constraints = [[0, [1, 1, 1]],
                   [1, [1, 1, 1]],
                   [2, [1, 1, 1]],
                   [3, [1, 1, 1]]]

    loads = [[32, -10000., 0.0, 0]]
            # [20, 1000., 0.0, 0],
            #[34, 1000., 0.0, 0],
           # [31, 10000., 0.0, 0]]
    poisson = 0.3
    youngs = 2000

    displacements = solve_full(elements, vertices, poisson, youngs, constraints, loads)
    magnitudes = get_displacement_magnitudes(displacements)
    print("magnitudes", magnitudes)
    display_tets(vertices, elements, magnitudes)

def test_pv_box():
    box = pv.Box((-1.0, 1.0, -5.0, 5.0, -1.0, 1.0))
    #box = examples.load_hexbeam()
    tet = tetgen.TetGen(box.triangulate())
    tet.tetrahedralize(order=1, mindihedral=20, minratio=2.0)

    print("tet: ", tet.mesh.points)
    grid = tet.grid
    verts = grid.points
    print("Cells: \n", grid.cells)
    print("Points: \n", verts)
    cell_arr = extract_tets(grid.cells)
    print(cell_arr)
    constraints = [[1, [1, 1, 1]],
                   [3, [1, 1, 1]],
                   [4, [1, 1, 1]],
                   [6, [1, 1, 1]]]

    loads = [[0, 10., 0.0, 0],
            [2, 10., 0.0, 0],
            [5, 10., 0.0, 0],
            [7, 10., 0.0, 0]]
    poisson = 0.3
    youngs = 2000

    displacements = solve_full(cell_arr, verts, poisson, youngs, constraints, loads)
    print(pd.DataFrame(displacements))
    magnitudes = get_displacement_magnitudes(displacements)

    display_tets(verts, cell_arr, magnitudes)

def main():
    test_box_grid()

if __name__ == "__main__":
    main()