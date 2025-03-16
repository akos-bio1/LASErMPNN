#!/usr/bin/env python3
"""
Inference script for LASErMPNN model.
Once model is trained, this script can be used to run inference on a given PDB file.

Benjamin Fry (bfry@g.harvard.edu)
"""

from pathlib import Path
import numpy as np
import torch
import torch.nn as nn
import argparse
import prody as pr
import torch.nn.functional as F
from prody.measure.measure import calcPhi, calcPsi
from collections import defaultdict
from dataclasses import dataclass, field

from typing import Tuple, List, Optional, Union

from utils.model import LASErMPNN, Sampled_Output
from utils.pdb_dataset import BatchData, UnprocessedLigandData, idealize_backbone_coords
from utils.constants import MAX_NUM_RESIDUE_ATOMS, aa_short_to_idx, aa_idx_to_short, aa_idx_to_long, aa_to_chi_angle_atom_index, dataset_atom_order, aa_long_to_short, atom_to_atomic_number, hydrogen_extended_dataset_atom_order 


CURR_FILE_DIR_PATH = Path(__file__).parent


class LigandInfo:
    """
    Stores the coordinates of ligand atoms.
    Use this information to generate masks for what atoms we should use for training.
    """
    def __init__(
        self, 
        atom_coords: Optional[torch.Tensor] = None, 
        atom_elements: Optional[List[str]] = None,
        atom_names: Optional[List[str]] = None,
        residue_identifier: Optional['ResidueIdentifier'] = None
    ):
        if atom_coords is not None and atom_elements is not None:

            assert len(atom_coords) == len(atom_elements), "Atom coordinates and elements must have the same length."
            assert (len(atom_coords) == len(atom_names)) if atom_names is not None else True, "Atom coordinates and names must have the same length."
            assert residue_identifier is not None, "Residue identifier must be provided if atom coordinates are given."

            self.atom_coords = atom_coords
            self.atom_elements = atom_elements
            self.ligand_subbatch_indices = torch.zeros(atom_coords.shape[0], dtype=torch.long)
            self.residue_identifiers = [residue_identifier]
            if atom_names is None:
                self.atom_names = atom_elements
            else:
                self.atom_names = atom_names
        else:
            self.atom_coords = torch.empty(0, 3)
            self.atom_elements = []
            self.atom_names = []
            self.ligand_subbatch_indices = torch.empty(0, dtype=torch.long)
            self.residue_identifiers = []

        self.max_subbatch_index = self.ligand_subbatch_indices.max().item() if self.ligand_subbatch_indices.numel() > 0 else -1

    def add_ligand(self, other_ligand: 'LigandInfo'):
        """
        Add ligand coordinates to the ligand info object.
        """
        assert other_ligand.max_subbatch_index == 0, "TODO: Implement adding LigandInfo with subbatch indices already set."
        self.atom_coords = torch.cat([self.atom_coords, other_ligand.atom_coords], dim=0)
        self.ligand_subbatch_indices = torch.cat([self.ligand_subbatch_indices, other_ligand.ligand_subbatch_indices + self.max_subbatch_index + 1], dim=0)
        self.atom_elements.extend(other_ligand.atom_elements)
        self.atom_names.extend(other_ligand.atom_names)
        self.residue_identifiers.extend(other_ligand.residue_identifiers)
        self.max_subbatch_index += 1
    
    def to_unprocessed_ligand_data(self, num_copies: int = 1) -> UnprocessedLigandData:
        """
        Convert the ligand info to an UnprocessedLigandData object.
        """
        assert num_copies >= 1, "Number of copies must be at least 1."

        lig_coords = self.atom_coords.float()
        lig_atomic_numbers = torch.tensor([atom_to_atomic_number[el.capitalize()] for el in self.atom_elements], dtype=torch.long)
        lig_batch = torch.zeros(self.atom_coords.shape[0], dtype=torch.long)
        lig_subbatch = self.ligand_subbatch_indices

        if num_copies == 1:
            output = UnprocessedLigandData(
                lig_coords = lig_coords,
                lig_atomic_numbers = lig_atomic_numbers,
                lig_batch_indices = lig_batch,
                lig_subbatch_indices = lig_subbatch,
            )
        else:
            output = UnprocessedLigandData(
                lig_coords = torch.cat([lig_coords for _ in range(num_copies)], dim=0), 
                lig_atomic_numbers = torch.cat([lig_atomic_numbers for _ in range(num_copies)], dim=0),
                lig_batch_indices = torch.cat([lig_batch + idx for idx in range(num_copies)], dim=0),
                lig_subbatch_indices= torch.cat([lig_subbatch for _ in range(num_copies)], dim=0),
            )
        return output
    
    def __repr__(self):
        return f'LigandInfo(atom_coords={self.atom_coords}, atom_elements={self.atom_elements}, ligand_subbatch_indices={self.ligand_subbatch_indices}, residue_identifiers={self.residue_identifiers})'


@dataclass
class ResidueIdentifier:
    """
    Stores the residue identifier.
    """
    segname: str
    chain_id: str
    resnum: int
    icode: str
    resname: str

    def __repr__(self):
        segname_repr = f'"{self.segname}"' if self.segname else r"''"
        icode_repr = f'"{self.icode}"' if self.icode else r"''"
        return f'({segname_repr}, {self.chain_id}, {self.resnum}, {icode_repr}, {self.resname})'


@dataclass
class ProteinComplexData:
    """
    Stores the chain data.
    """
    prody_protein: pr.HierView
    pdb_code: str

    use_input_water: bool = False
    contains_water: bool = False
    verbose: bool = True

    # Residue-level metadata.
    sequence_indices: torch.Tensor = field(init=False)
    heavy_atom_coords: torch.Tensor = field(init=False)
    phi_psi_angles: torch.Tensor = field(init=False)
    residue_identifiers: List[ResidueIdentifier] = field(init=False)
    chain_indices: torch.Tensor = field(init=False)
    fixed_rotamers: torch.Tensor = field(init=False)

    # Ligand metadata.
    ligand_info: LigandInfo = field(default_factory=LigandInfo)

    def __post_init__(self):
        all_data = defaultdict(list)
        for idx, segchain in enumerate(self.prody_protein):
            assert isinstance(segchain, pr.atomic.chain.Chain), "Prody object is not a chain."

            # Track residue-level metadata.
            for residue in segchain:
                resname, resnum, icode, bfac = residue.getResname(), residue.getResnum(), residue.getIcode(), residue.getBetas()
                residue_identifier = ResidueIdentifier(segchain.getSegname(), segchain.getChid(), resnum, icode, resname) # type: ignore

                if resname == 'HOH':
                    if not self.contains_water and not self.use_input_water:
                        if self.verbose: print(f'Found water in {self.pdb_code}, it is currently being ignored.')
                        self.contains_water = True
                    elif self.use_input_water:
                        self.ligand_info.add_ligand(create_ligand_info(residue, residue_identifier))

                elif is_amino_acid(residue) and not any(residue.getFlags('hetatm')):
                    olc_resname = 'X'
                    if resname in aa_long_to_short:
                        olc_resname = aa_long_to_short[resname]

                    bfac_max = max(bfac)

                    residue_coords, residue_sequence_index = get_residue_coords(residue, olc_resname)

                    if residue_coords[:3].isnan().any().item():
                        if self.verbose: print(residue, 'is missing backbone (N, CA, C) atoms. Dropping it from the structure...')
                        continue

                    phi, psi = compute_phi_psi_angles(residue) # type: ignore
                    all_data['sequence_indices'].append(residue_sequence_index)
                    all_data['heavy_atom_coords'].append(residue_coords)
                    all_data['residue_identifiers'].append(residue_identifier)
                    all_data['phi_psi_angles'].append(torch.tensor([phi, psi], dtype=torch.float))
                    all_data['chain_indices'].append(torch.tensor(idx, dtype=torch.long))
                    all_data['fixed_rotamers'].append(torch.tensor((bool(np.isclose(bfac_max, 1.0))), dtype=torch.bool))
                else:
                    self.ligand_info.add_ligand(create_ligand_info(residue, residue_identifier))

        # Convert lists to tensors where necessary.
        for key, value in all_data.items():
            if key in ['sequence_indices', 'heavy_atom_coords', 'phi_psi_angles', 'chain_indices', 'fixed_rotamers']:
                value = torch.stack(value, dim=0)
            setattr(self, key, value)
        
    def compute_backbone_coords(self) -> torch.Tensor:
        heavy_atom_coords = self.heavy_atom_coords.clone()

        backbone_coords = heavy_atom_coords.gather(1, torch.tensor([[0, 1, 4, 2, 3]]).unsqueeze(-1).expand(self.heavy_atom_coords.shape[0], -1, 3))
        idealized_backbone_coords = idealize_backbone_coords(backbone_coords, self.phi_psi_angles)
        heavy_atom_coords[:, :5] = idealized_backbone_coords.gather(1, torch.tensor([[0, 1, 3, 4, 2]]).unsqueeze(-1).expand(self.heavy_atom_coords.shape[0], -1, 3))

        return idealized_backbone_coords, heavy_atom_coords
    
    def compute_chi_angles(self) -> torch.Tensor:
        """
        Compute the chi angles for a given set of residue coordinates and identities.

        Args:
            coordinate_matrix (torch.Tensor): The coordinate matrix containing the (N, 14, 3) atomic coordinates.
            residue_indices (torch.Tensor dtype: long): The indices of the residues for which to compute the chi angles.

        Returns:
            torch.Tensor: The computed chi angles.
        """
        coordinate_matrix = self.heavy_atom_coords
        residue_indices = self.sequence_indices

        # Initialize output tensor.
        output = torch.full((coordinate_matrix.shape[0], 4), torch.nan, dtype=torch.float, device=coordinate_matrix.device)

        # Handle unknown residues (just treat them as GLY/no chi angles).
        x_residue_mask = residue_indices == aa_short_to_idx['X']
        coordinate_matrix = coordinate_matrix[~x_residue_mask]
        residue_indices = residue_indices[~x_residue_mask]

        # Handle no residues after masking for unknown residues. Without this the F.pad line below fails to handle empty tensor.
        if coordinate_matrix.shape[0] == 0:
            return output

        # Expand coordinates to (N, 15, 3) for easy indexing.
        nan_padded_coords = F.pad(coordinate_matrix, (0, 0, 0, 1, 0, 0), 'constant', torch.nan)
        expanded_sidechain_coord_indices = aa_to_chi_angle_atom_index[residue_indices]

        # Gather coordinates for chi angle computation as (N, 4, 4, 3) tensor.
        chi_coords_stacked = nan_padded_coords.gather(1, expanded_sidechain_coord_indices.flatten(start_dim=1).unsqueeze(-1).expand(-1, -1, 3)).reshape(residue_indices.shape[0], 4, 4, 3)

        # Compute batched chi-angles: 
        # https://en.wikipedia.org/w/index.php?title=Dihedral_angle&oldid=689165217#Angle_between_three_vectors
        b0 = chi_coords_stacked[:, :, 0, :] - chi_coords_stacked[:, :, 1, :]
        b1 = chi_coords_stacked[:, :, 1, :] - chi_coords_stacked[:, :, 2, :]
        b2 = chi_coords_stacked[:, :, 2, :] - chi_coords_stacked[:, :, 3, :]
        n1 = torch.cross(b0, b1)
        n2 = torch.cross(b1, b2)
        m1 = torch.cross(n1, b1 / torch.linalg.vector_norm(b1, dim=2, keepdim=True))
        x = torch.sum(n1 * n2, dim=-1)
        y = torch.sum(m1 * n2, dim=-1)

        chi_angles = torch.rad2deg(torch.arctan2(y, x))

        # Update output tensor with computed chi angles.
        output[~x_residue_mask] = chi_angles

        return output
    
    def output_batch_data(self, fix_beta: bool = False, num_copies: int = 1) -> BatchData:
        """
        Output the batch data. May duplicate the data with num_copies > 1 to run multiple designs in parallel.
        """

        bb_coords, self.heavy_atom_coords = self.compute_backbone_coords()
        chi_angles = self.compute_chi_angles()
        seq_indices = torch.arange(self.sequence_indices.shape[0], dtype=torch.long)
        msa_data = torch.empty(self.sequence_indices.shape[0], 21, 0, dtype=torch.long)

        ligand_info = self.ligand_info.to_unprocessed_ligand_data(num_copies)

        zeros_long = torch.zeros(self.sequence_indices.shape[0], dtype=torch.long)
        zeros_float = torch.zeros(self.sequence_indices.shape[0], dtype=torch.float)
        zeros_bool = torch.zeros(self.sequence_indices.shape[0], dtype=torch.bool)

        if fix_beta:
            chain_mask = self.fixed_rotamers
        else:
            chain_mask = zeros_bool
        assert num_copies >= 1, "Number of copies must be at least 1."

        if num_copies == 1:
            batch_data_dict = {
                'pdb_codes': [self.pdb_code],
                'sequence_indices': self.sequence_indices,
                'chi_angles': chi_angles,
                'backbone_coords': bb_coords,
                'phi_psi_angles': self.phi_psi_angles,
                'sidechain_contact_number': zeros_long,
                'residue_burial_counts': zeros_long, # TODO: Implement residue burial counts for analysis.
                'batch_indices': zeros_long,
                'chain_indices': self.chain_indices,
                'sampled_chain_mask': zeros_bool,
                'resnum_indices': seq_indices,
                'chain_mask': chain_mask,
                'extra_atom_contact_mask': zeros_bool,
                'msa_data': msa_data,
                'msa_depth_weight': zeros_float,
                'unprocessed_ligand_input_data': ligand_info,
                'first_shell_ligand_contact_mask': zeros_bool,
                'sc_mediated_hbond_counts': zeros_long,
            }
        else:
            batch_data_dict = {
                'pdb_codes': [self.pdb_code for _ in range(num_copies)],
                'sequence_indices': torch.cat([self.sequence_indices for _ in range(num_copies)], dim=0),
                'chi_angles': torch.cat([chi_angles for _ in range(num_copies)], dim=0),
                'backbone_coords': torch.cat([bb_coords for _ in range(num_copies)], dim=0),
                'phi_psi_angles': torch.cat([self.phi_psi_angles for _ in range(num_copies)], dim=0),
                'sidechain_contact_number': torch.cat([zeros_long for _ in range(num_copies)], dim=0),
                'residue_burial_counts': torch.cat([zeros_long for _ in range(num_copies)], dim=0), # TODO: Implement residue burial counts for analysis.
                'batch_indices': torch.cat([zeros_long + idx for idx in range(num_copies)], dim=0),
                'chain_indices': torch.cat([self.chain_indices for _ in range(num_copies)], dim=0),
                'resnum_indices': torch.cat([seq_indices for _ in range(num_copies)], dim=0),
                'chain_mask': torch.cat([chain_mask for _ in range(num_copies)], dim=0),
                'sampled_chain_mask': torch.cat([zeros_bool for _ in range(num_copies)], dim=0),
                'extra_atom_contact_mask': torch.cat([zeros_bool for _ in range(num_copies)], dim=0),
                'msa_data': torch.cat([msa_data for _ in range(num_copies)], dim=0),
                'msa_depth_weight': torch.cat([zeros_float for _ in range(num_copies)], dim=0),
                'unprocessed_ligand_input_data': ligand_info,
                'first_shell_ligand_contact_mask': torch.cat([zeros_bool for _ in range(num_copies)], dim=0),
                'sc_mediated_hbond_counts': torch.cat([zeros_long for _ in range(num_copies)], dim=0),
            }

        return BatchData(**batch_data_dict)
    

def compute_phi_psi_angles(residue: pr.AtomGroup) -> Tuple[float, float]:
    """
    Given a prody residue object, calculate the phi and psi angles.
    If the angles cannot be calculated, return NaN.
    """
    try:
        phi_angle = calcPhi(residue)
    except ValueError:
        phi_angle = torch.nan

    try:
        psi_angle = calcPsi(residue)
    except ValueError:
        psi_angle = torch.nan
    
    return phi_angle, psi_angle


def get_padded_residue_coordinate_tensor():
    """
    Returns a tensor of shape (MAX_NUM_ATOMS, 3) filled with NaNs.
    """
    return torch.full((MAX_NUM_RESIDUE_ATOMS, 3), torch.nan)


def get_residue_coords(residue: pr.AtomGroup, residue_olc: str) -> Tuple[torch.Tensor, torch.Tensor]:
    """
    Given a prody residue object, return the coordinates of all atoms in the residue.
    """

    residue_coords = get_padded_residue_coordinate_tensor()
    res_atom_order = dataset_atom_order[residue_olc]

    for atom, coord in zip(residue.getNames(), residue.getCoords()): # type: ignore
        if atom in res_atom_order:
            residue_coords[res_atom_order.index(atom)] = torch.from_numpy(coord)

    return residue_coords, torch.tensor(aa_short_to_idx[residue_olc], dtype=torch.long)


def is_amino_acid(res: pr.Residue) -> bool:
    """
    Given a prody residue object, return whether the residue is an amino acid.
    """
    if res.getResname() in aa_long_to_short:
        return True
    atom_names = set(res.copy().getNames())
    if all(x in atom_names  for x in ['N', 'CA', 'C', 'O']):
        return True
    return False


def create_ligand_info(residue: pr.AtomGroup, residue_code: ResidueIdentifier) -> LigandInfo:
    """
    Given a prody residue object, return a LigandInfo object containing the residue's atom coordinates.
    """

    residue_elements = residue.getElements() # type: ignore
    if not any(residue_elements):
        residue_elements = [''.join([y for y in x if not y.isnumeric()]) for x in residue.getNames()] # type: ignore
    
    return LigandInfo(torch.from_numpy(residue.getCoords()), list(residue_elements), list(residue.getNames()), residue_code) # type: ignore


def get_protein_hierview(path_to_pdb: str) -> pr.HierView:
    """
    Given a path to a PDB file, return a prody HierView object representing the protein structure.
    """
    protein = pr.parsePDB(path_to_pdb)
    assert isinstance(protein, pr.AtomGroup), "Prody parsePDB failed to return an AtomGroup"
    assert isinstance(protein.protein, pr.atomic.selection.Selection), "Prody object has no protein residues."

    return protein.getHierView()


def load_model_from_parameter_dict(path_to_weights: str, inference_device: Union[str, torch.device], strict: bool = True) -> Tuple[LASErMPNN, dict]:
    """
    Given a path to a model weights file and the device to run inference on, 
    load the model and training parameters
    """
    # Load the model and training parameters on the target device
    loaded_dict = torch.load(path_to_weights, map_location=inference_device, weights_only=True)

    # Extract the model and training parameters
    training_parameter_dict = loaded_dict['params']
    model_state_dict = loaded_dict['model_state_dict']
    ligand_encoder_params = loaded_dict['ligand_encoder_params']

    # Load the model
    model = LASErMPNN(ligand_encoder_params=ligand_encoder_params, **training_parameter_dict['model_params']).to(inference_device)
    model.load_state_dict(model_state_dict, strict=strict)

    model.eval()

    return model, training_parameter_dict


@torch.no_grad()
def sample_model(
    model: LASErMPNN, batch_data: BatchData, sequence_temp: Optional[float], bb_noise: float, params: dict, use_edo: bool = False, chi_temp: Optional[float] = None,
    verbose: bool = False, disable_pbar: bool = False, fs_sequence_temp: Optional[float] = None, chi_min_p: float = 0.0, seq_min_p: float = 0.0,
    ignore_chain_mask_zeros: bool = False, disabled_residues: Optional[List[str]] = ['X'], disable_charged_first_shell: bool = False, repack_all: bool = False
) -> Sampled_Output:
    """
    Given a model and batch data, return the model's sampled output.
        No gradients are calculated during this process.
    """
    model.eval()

    batch_data.to_device(model.device)
    # if disable_charged_first_shell:
    # batch_data.sequence_indices  = torch.full(batch_data.sequence_indices.shape, fill_value=aa_short_to_idx['X'], device=model.device, dtype=torch.long)
    # batch_data.sequence_indices = torch.full_like(batch_data.sequence_indices, fill_value=aa_short_to_idx['A'], device=model.device, dtype=torch.long)
    # batch_data.recompute_cb_atoms() # this isn't necesary since ProteinComplexData already does it when you call output_batch_data
    batch_data.construct_graphs(
        model.rotamer_builder,
        model.ligand_featurizer, 
        **params['model_params']['graph_structure'],
        protein_training_noise = bb_noise,
        ligand_training_noise = 0.0,
        subgraph_only_dropout_rate = 0.0,
        num_adjacent_residues_to_drop = 0,
        build_hydrogens = params['model_params']['build_hydrogens'],
    )

    first_shell_and_buried = None
    if disable_charged_first_shell:
        # Any first shell & buried residue logits are warped to prevent charged residues from being in the first shell.
        first_shell_and_buried = (batch_data.first_shell_ligand_contact_mask & batch_data.sidechain_in_hull_mask)

    if not use_edo:
        if verbose: print("Using random decoding order.")
        batch_data.generate_decoding_order(stack_tensors=True)
        sample_temperature_vector = None

        if fs_sequence_temp is not None:
            sequence_temp = 1e-6 if sequence_temp is None else sequence_temp
            sample_temperature_vector = torch.full((batch_data.num_residues,), sequence_temp, dtype=torch.float, device=model.device)
            # TODO: Use a distance cutoff definition of fs_sequence_temp instead of first shell ligand contact mask.
            sample_temperature_vector[batch_data.first_shell_ligand_contact_mask] = fs_sequence_temp

        sampling_output = model.sample(
            batch_data, 
            sequence_sample_temperature=sample_temperature_vector if sample_temperature_vector is not None else sequence_temp, 
            chi_angle_sample_temperature=chi_temp, disable_pbar=disable_pbar, chi_min_p=chi_min_p, seq_min_p=seq_min_p,
            ignore_chain_mask_zeros=ignore_chain_mask_zeros, disabled_residues=disabled_residues, 
            disable_charged_residue_mask=first_shell_and_buried, repack_all=repack_all
        )

    else:
        assert not disable_charged_first_shell, 'Not yet implemented for LDO.'

        sampling_output, sampled_dorder, t_probs, t_ent = model.sample_by_lowest_entropy( # type: ignore
            batch_data, sequence_sample_temperature=sequence_temp, disabled_residues=disabled_residues
        )
    
    return sampling_output


def output_ligand_structure(ligand_info: LigandInfo) -> pr.AtomGroup:
    """
    Given a ligand info object, output the ligand ProDy AtomGroup object.
    """
    all_info = defaultdict(list)
    for coord, element, name, sbidx in zip(ligand_info.atom_coords, ligand_info.atom_elements, ligand_info.atom_names, ligand_info.ligand_subbatch_indices):
        all_info['coords'].append(coord.cpu().numpy() if isinstance(coord, torch.Tensor) else coord)
        all_info['atom_names'].append(name)
        all_info['elements'].append(element)
        all_info['resnames'].append(ligand_info.residue_identifiers[sbidx].resname) # type: ignore
        all_info['segnames'].append(ligand_info.residue_identifiers[sbidx].segname) # type: ignore
        all_info['chids'].append(ligand_info.residue_identifiers[sbidx].chain_id) # type: ignore
        all_info['resnums'].append(ligand_info.residue_identifiers[sbidx].resnum) # type: ignore
        all_info['icodes'].append(ligand_info.residue_identifiers[sbidx].icode) # type: ignore
        all_info['occupancies'].append(1.0)

    ligand = pr.AtomGroup('LASErMPNN Generated Ligand')
    coords = np.array(all_info['coords'])
    ligand.setCoords(coords)
    ligand.setNames(all_info['atom_names']) # type: ignore
    ligand.setResnames(all_info['resnames']) # type: ignore
    ligand.setSegnames(all_info['segnames']) # type: ignore
    ligand.setChids(all_info['chids']) # type: ignore
    ligand.setResnums(all_info['resnums']) # type: ignore
    ligand.setIcodes(all_info['icodes']) # type: ignore
    ligand.setOccupancies(all_info['occupancies']) # type: ignore
    ligand.setElements(all_info['elements']) # type: ignore

    return ligand


def output_protein_structure_numpy(full_atom_coords: np.ndarray, amino_acid_indices: np.ndarray, residue_metadata: List[ResidueIdentifier], nh_coords: np.ndarray) -> pr.AtomGroup:
    """
    Write the protein structure to a ProDy AtomGroup Object starting from numpy arrays.
    """

    assert len(full_atom_coords) == len(amino_acid_indices), f"Coordinates and sequence indices must have the same number of elements {full_atom_coords.shape}, {amino_acid_indices.shape}"
    assert len(full_atom_coords) == len(residue_metadata), f"Input lists must be the same length {full_atom_coords.shape}, {len(amino_acid_indices)}"
    assert len(full_atom_coords) == len(nh_coords), f"Coordinates and NH coordinates must have the same number of elements {full_atom_coords.shape}, {nh_coords.shape}"

    atom_order_dict = hydrogen_extended_dataset_atom_order if full_atom_coords.shape[1] == 24 else dataset_atom_order

    atom_features = defaultdict(list)
    for coord, sequence_index, identifier, nh_coord in zip(full_atom_coords, amino_acid_indices, residue_metadata, nh_coords):

        coord_mask = np.isnan(coord).any(axis=1)
        coord = coord[~coord_mask]
        nh_coord_mask = np.isnan(nh_coord).any(axis=-1)
        nh_coord = nh_coord[~nh_coord_mask]
        atom_names = [x for idx, x in enumerate(atom_order_dict[aa_idx_to_short[int(sequence_index.item())]]) if not coord_mask[idx].item()]
        if (~nh_coord_mask).any():
            atom_names.extend(['H'])
            coord = np.concatenate([coord, nh_coord], axis=0)

        atom_features['atom_labels'].extend(atom_names)
        atom_features['resnames'].extend([aa_idx_to_long[int(sequence_index.item())]] * len(atom_names))

        atom_features['betas'].extend([1.0] * len(atom_names))
        atom_features['segnames'].extend([identifier.segname] * len(atom_names))
        atom_features['chains'].extend([identifier.chain_id] * len(atom_names))
        atom_features['resnums'].extend([identifier.resnum] * len(atom_names))
        atom_features['icodes'].extend([identifier.icode] * len(atom_names))

        atom_features['occupancies'].extend([1.0] * len(atom_names))
        atom_features['coords'].extend(coord)

    protein = pr.AtomGroup('LASErMPNN Generated Protein')
    protein.setCoords(atom_features['coords'])
    protein.setNames(atom_features['atom_labels']) # type: ignore
    protein.setResnames(atom_features['resnames']) # type: ignore
    protein.setSegnames(atom_features['segnames']) # type: ignore
    protein.setChids(atom_features['chains']) # type: ignore
    protein.setResnums(atom_features['resnums']) # type: ignore
    protein.setIcodes(atom_features['icodes']) # type: ignore
    protein.setOccupancies(atom_features['occupancies']) # type: ignore
    protein.setBetas(atom_features['betas']) # type: ignore 
    protein.setElements([x[0] for x in atom_features['atom_labels']]) # type: ignore

    return protein


def output_protein_structure(full_atom_coords: torch.Tensor, amino_acid_indices: torch.Tensor, residue_metadata: List[ResidueIdentifier], nh_coords: torch.Tensor, bfactors: Optional[torch.Tensor] = None) -> pr.AtomGroup:
    """
    Write the protein structure to a ProDy AtomGroup Object.
    """

    assert len(full_atom_coords) == len(amino_acid_indices), f"Coordinates and sequence indices must have the same number of elements {full_atom_coords.shape}, {amino_acid_indices.shape}"
    assert len(full_atom_coords) == len(residue_metadata), f"Input lists must be the same length {full_atom_coords.shape}, {len(amino_acid_indices)}"
    assert len(full_atom_coords) == len(nh_coords), f"Coordinates and NH coordinates must have the same number of elements {full_atom_coords.shape}, {nh_coords.shape}"

    if bfactors is not None:
        assert len(full_atom_coords) == len(bfactors), f"Coordinates and B-factors must have the same number of elements {full_atom_coords.shape}, {bfactors.shape}"

    atom_order_dict = hydrogen_extended_dataset_atom_order if full_atom_coords.shape[1] == 24 else dataset_atom_order
    if bfactors is None:
        bfactors = torch.ones(full_atom_coords.shape[0], dtype=torch.float)

    atom_features = defaultdict(list)
    for coord, sequence_index, identifier, nh_coord, b in zip(full_atom_coords, amino_acid_indices, residue_metadata, nh_coords, bfactors.cpu().numpy()):
        coord_mask = coord.isnan().any(dim=1)
        coord = coord[~coord_mask]

        nh_coord_mask = nh_coord.isnan().any(dim=-1)
        nh_coord = nh_coord[~nh_coord_mask]
        atom_names = [x for idx, x in enumerate(atom_order_dict[aa_idx_to_short[int(sequence_index.item())]]) if not coord_mask[idx].item()]
        if (~nh_coord_mask).any():
            atom_names.extend(['H'])
            coord = torch.cat([coord, nh_coord], dim=0)
        atom_features['atom_labels'].extend(atom_names)
        atom_features['resnames'].extend([aa_idx_to_long[int(sequence_index.item())]] * len(atom_names))

        atom_features['betas'].extend([b.item()] * len(atom_names))
        atom_features['segnames'].extend([identifier.segname] * len(atom_names))
        atom_features['chains'].extend([identifier.chain_id] * len(atom_names))
        atom_features['resnums'].extend([identifier.resnum] * len(atom_names))
        atom_features['icodes'].extend([identifier.icode] * len(atom_names))

        atom_features['occupancies'].extend([1.0] * len(atom_names))
        atom_features['coords'].extend(coord.cpu().numpy())

    protein = pr.AtomGroup('LASErMPNN Generated Protein')
    protein.setCoords(atom_features['coords'])
    protein.setNames(atom_features['atom_labels']) # type: ignore
    protein.setResnames(atom_features['resnames']) # type: ignore
    protein.setSegnames(atom_features['segnames']) # type: ignore
    protein.setChids(atom_features['chains']) # type: ignore
    protein.setResnums(atom_features['resnums']) # type: ignore
    protein.setIcodes(atom_features['icodes']) # type: ignore
    protein.setOccupancies(atom_features['occupancies']) # type: ignore
    protein.setBetas(atom_features['betas']) # type: ignore 
    protein.setElements([x[0] for x in atom_features['atom_labels']]) # type: ignore
    return protein


def run_inference(input_pdb_path: str, path_to_weights: str, sequence_temp: Optional[float], bb_noise: float, inference_device: str, fix_beta: bool, output_path: str, strict_load: bool, use_edo: bool, fs_sequence_temp: Optional[float], repack_only: bool, ignore_ligand: bool):
    print("Loading", input_pdb_path, "and running inference with", path_to_weights, "on", inference_device)
    if bb_noise > 0:
        print(f"Noising backbone with {bb_noise}A Gaussian noise.")

    # Parse the input PDB file.
    protein_hv = get_protein_hierview(input_pdb_path)

    if fix_beta:
        unique_betas = np.unique(np.concatenate([x.getBetas() for x in protein_hv.iterChains()])) # type: ignore
        assert len(unique_betas) <= 2 and np.isin(unique_betas, np.array([0.0, 1.0])).all(), f"Only B-Factors 0.0 and 1.0 are allowed when fixing residues with -b flag, please adjust your input. Found b-factors: {unique_betas}"
    
    data = ProteinComplexData(protein_hv, input_pdb_path)
    batch_data = data.output_batch_data(fix_beta)

    if ignore_ligand:
        raise NotImplementedError

    # Report the parsing results.
    if repack_only:
        print("Repacking all residues with fixed sequence.")
        batch_data.chain_mask = torch.ones_like(batch_data.chain_mask)
    else:
        print(f"Designing {batch_data.num_residues - batch_data.chain_mask.sum()} Residues. Fixing {batch_data.chain_mask.sum()} Residues.")

    print("Parsed ligands: ", [(x.resname, x.resnum)  for x in data.ligand_info.residue_identifiers]) # type: ignore

    # Load the model and run inference
    model, params = load_model_from_parameter_dict(path_to_weights, inference_device, strict=strict_load)
    sampled_output = sample_model(model, batch_data, sequence_temp, bb_noise, params, use_edo, fs_sequence_temp=fs_sequence_temp, repack_all=repack_only) 

    sampled_probs = sampled_output.sequence_logits.softmax(dim=-1).gather(1, sampled_output.sampled_sequence_indices.unsqueeze(-1)).squeeze(-1)

    # Write the output to a PDB file.
    full_atom_coords = model.rotamer_builder.build_rotamers(batch_data.backbone_coords, sampled_output.sampled_chi_degrees, sampled_output.sampled_sequence_indices, add_nonrotatable_hydrogens=True)
    assert isinstance(full_atom_coords, torch.Tensor), "Rotamer builder failed to return a tensor."
    nh_coords = model.rotamer_builder.impute_backbone_nh_coords(full_atom_coords.float(), sampled_output.sampled_sequence_indices, batch_data.phi_psi_angles[:, 0].unsqueeze(-1))
    full_atom_coords = model.rotamer_builder.cleanup_titratable_hydrogens(full_atom_coords.float(), sampled_output.sampled_sequence_indices, nh_coords, batch_data, model.hbond_network_detector) # type: ignore

    out_prot = output_protein_structure(full_atom_coords, sampled_output.sampled_sequence_indices, data.residue_identifiers, nh_coords, sampled_probs)
    try:
        out_lig = output_ligand_structure(data.ligand_info)
        out_prot += out_lig
    except:
        pass

    pr.writePDB(output_path, out_prot)
    print("Wrote laser designed output to", output_path)


def parse_args():
    parser = argparse.ArgumentParser(description='Run LASErMPNN inference on a given PDB file.')
    # Required input:
    parser.add_argument('input_pdb_code', type=str, help='Path to the input PDB file.')
    parser.add_argument('--model_weights', '-w', dest='model_weights', type=str, default=str(CURR_FILE_DIR_PATH / 'model_weights/laser_weights_0p1A_noise_ligandmpnn_split.pt'), help='Path to dictionary of torch.save()ed model state_dict and training parameters.')
    # Optional input:
    parser.add_argument('--output_path', '-o', dest='output_path', type=str, default='laser_output.pdb', help='Path to the output PDB file.')
    parser.add_argument('--temp', '-t', dest='sequence_temp', type=str, default='', help='Sequence sample temperature.')
    parser.add_argument('--fs_sequence_temp', '-f', dest='fs_sequence_temp', type=float, default=None, help='Residues around the ligand will be sampled at this temperature, otherwise they default to sequence_temp.')
    parser.add_argument('--bb_noise', '-n', dest='backbone_noise', type=str, default='', help='Inference backbone noise.')
    parser.add_argument('--device', '-d', dest='device', type=str, default='cpu', help='Pytorch style device string. Ex: "cuda:0" or "cpu".')
    # Flags:
    parser.add_argument('--fix_beta', '-b', dest='fix_beta', action='store_true', help='Residues with B-Factor of 1.0 have sequence and rotamer fixed, residues with B-Factor of 0.0 are designed.')
    parser.add_argument('--ignore_statedict_mismatch', '-s', dest='strict_load', action='store_false', help='Small state_dict mismatches are ignored. Don\'t use this unless any missing parameters aren\'t learned during training.')
    parser.add_argument('--ldo', '-l', dest='learned_decorder', action='store_true', help='Uses learned decoding order.')
    parser.add_argument('--ebd', '-e', dest='entropy_decoder', action='store_true', help='Uses entropy based decoding order. Decodes all residues and selects the lowest entropy residue as next to decode, then recomputes all remaining residues. Takes longer than normal decoding.')
    parser.add_argument('--repack_only', action='store_true', help='Only repack residues, do not design new ones.')
    parser.add_argument('--ignore_ligand', action='store_true', help='Ignore ligands in the input PDB file.')

    out = parser.parse_args()

    seq_temp = float(out.sequence_temp) if out.sequence_temp else None
    backbone_noise = float(out.backbone_noise) if out.backbone_noise else 0.0
    assert not (out.learned_decorder and out.entropy_decoder), "Cannot use both learned decoding order and entropy based decoding order."
    return out, seq_temp, backbone_noise


if __name__ == "__main__":
    parsed_args, sequence_temp, backbone_noise = parse_args() 
    run_inference(
        parsed_args.input_pdb_code, parsed_args.model_weights, sequence_temp, backbone_noise, 
        parsed_args.device, parsed_args.fix_beta, parsed_args.output_path, parsed_args.strict_load, 
        parsed_args.entropy_decoder, parsed_args.fs_sequence_temp,
        repack_only=parsed_args.repack_only, ignore_ligand=parsed_args.ignore_ligand
    )

