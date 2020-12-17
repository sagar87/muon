from collections import OrderedDict
from typing import Optional, Union
import numpy as np
import pandas as pd
import logging
from datetime import datetime
from tqdm import tqdm
from scipy.sparse import lil_matrix
from anndata import AnnData
from .._core.mudata import MuData
from . import utils as atacutils
from .._core.utils import get_gene_annotation_from_rna

#
# Fragments
#
# Fragments file is a BED-like file describing individual fragments.
# A single record in such a file typically includes 5 tab-separated fields:
#
# chr1 10000 11000 GTCAGTCAGTCAGTCA-1 1
# ^    ^     ^     ^                  ^
# |    |     |     |                  |
# |    |     |     4: name (cell barcode)
# |    |     3: end (3' fragment position, exclusive)
# |    2: start (5' fragment position, inclusive)|
# 1: contig (chromosome)              5: score (number of cuts per fragment)
#
# Fragments file is compressed (.gz) and has to be indexed
# with Tabix in order to be used (.gz.tbi).
#

def import_pysam():
    """Print helpful message if pysam not available"""
    try:
        import pysam
        return pysam
    except ImportError:
        raise ImportError(
            "pysam is not available. It is required to work with the fragments file. \
            Install pysam from PyPI (`pip install pysam`) \
            or from GitHub (`pip install git+https://github.com/pysam-developers/pysam`)"
        )


def open_fragment_connection(fragment_path):
    '''Imports pysam and opens connection with BED parser'''

    pysam = import_pysam()
    frag = pysam.TabixFile(fragment_path, parser=pysam.asBed())

    return frag

def locate_fragments(data: Union[AnnData, MuData], fragments: Optional[str] = None, return_fragments: bool = False):
    """
    Parse fragments file and add a variable to access it to the .uns["files"]["fragments"]

    Fragments file is never read to memory, and connection to the file is closed
    upon function completion.

    Parameters
    ----------
    data
            AnnData object with peak counts or multimodal MuData object with 'atac' modality.
    fragments
            A path to the compressed tab-separated fragments file (e.g. atac_fragments.tsv.gz).
    return_fragments
            If return the Tabix connection the fragments file. False by default.
    """
    frag = None
    try:
        adata = atacutils.fetch_atac_mod(data)

        if fragments is None:
            # Check if a path is already present
            if "fragments" in adata.uns["files"]:
                fragments = adata.uns["files"]["fragments"]
                print(adata.uns["files"]["fragments"])
            else:
                raise ValueError("No filepath found in .uns['files']['fragments'] and `fragments` argument is None. Please specify one of the two.")

        # Here we make sure we can create a connection to the fragments file
        frag = open_fragment_connection(fragments)

        if "files" not in adata.uns:
            adata.uns["files"] = OrderedDict()
        adata.uns["files"]["fragments"] = fragments

        if return_fragments:
            return frag

    except Exception as e:
        print(e)

    finally:
        if frag is not None and not return_fragments:
            # The connection has to be closed
            frag.close()


def count_fragments_features(
    data: Union[AnnData, MuData],
    features: Optional[pd.DataFrame] = None,
    extend_upstream: int = 2e3,
    extend_downstream: int = 0,
) -> AnnData:
    """
    Count fragments overlapping given Features. Returns cells x features matrix.

        Parameters
        ----------
        data
                AnnData object with peak counts or multimodal MuData object with 'atac' modality.
        features
                A DataFrame with feature annotation, e.g. genes.
                Annotation has to contain columns: Chromosome, Start, End.
        extend_upsteam
                Number of nucleotides to extend every gene upstream (2000 by default to extend gene coordinates to promoter regions)
        extend_downstream
                Number of nucleotides to extend every gene downstream (0 by default)
    """
    adata = atacutils.fetch_atac_mod(data)

    if features is None:
        # Try to gene gene annotation in the data.mod['rna']
        if (
            isinstance(data, MuData)
            and "rna" in data.mod
            and "interval" in data.mod["rna"].var.columns
        ):
            features = get_gene_annotation_from_rna(data)
        else:
            raise ValueError(
                "Argument `features` is required. It should be a BED-like DataFrame with gene coordinates and names."
            )

    n = adata.n_obs
    n_features = features.shape[0]

    # Dictionary with matrix positions
    d = {k: v for k, v in zip(adata.obs.index, range(n))}

    # Open connection to fragments file
    fragments = locate_fragments(adata, return_fragments=True)

    try:
        # List of lists matrix is quick and convenient to fill by row
        mx = lil_matrix((n_features, n), dtype=int)

        logging.info(
            f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Counting fragments in {n} cells for {features.shape[0]} features..."
        )

        for i in tqdm(range(n_features)):  # iterate over features (e.g. genes)
            f = features.iloc[i]
            for fr in fragments.fetch(
                f.Chromosome, f.Start - extend_upstream, f.End + extend_downstream
            ):
                try:
                    ind = d[fr.name]  # cell barcode (e.g. GTCAGTCAGTCAGTCA-1)
                    mx.rows[i].append(ind)
                    mx.data[i].append(int(fr.score))  # number of cuts per fragment (e.g. 2)
                except:
                    pass

        # Faster to convert to csr first and then transpose
        mx = mx.tocsr().transpose()

        return AnnData(X=mx, obs=adata.obs, var=features)

    except Exception as e:
        logging.error(e)
        raise e

    finally:
        # The connection has to be closed
        fragments.close()


def _region_pileup(mx, fragments, d, chromosome, start, end):
    """Add fragments to existing matrix"""
    n_features = mx.shape[1]

    for fr in fragments.fetch(
        chromosome, start, end
    ):
        try:
            rowind = d[fr.name]  # cell barcode (e.g. GTCAGTCAGTCAGTCA-1)
            score = int(fr.score)  # number of cuts per fragment (e.g. 2)
            colind_start = max(fr.start - start, 0)
            colind_end = min(fr.end - start, n_features)  # ends are non-inclusive in bed
            mx[rowind, colind_start:colind_end] += score
        except:
            pass


def region_pileup(
        fragments: Union[str, "pysam.libctabix.TabixFile"],
        cells: np.array,
        chromosome: str,
        start: int,
        end: int
) -> AnnData:
    """
    Pile up reads in regions. Returns a cell x position `AnnData` object that can be used for QC.

    Parameters
    ----------
    fragments
        Path to or `pysam` connection to a tabix indexed fragments file.
    cells
        List of cells to fetch
    chromosome
        Name of the chromosome to extract
    start
        Start position
    end
        End position
    """

    if isinstance(fragments, str):
        fragments = open_fragment_connection(fragments)

    n = cells.shape[0]
    n_features = end - start
    if n_features < 0:
        raise ValueError(f"Start must be smaller than end. (Start = {start}, End = {end})")

    # Dictionary with matrix positions
    d = {k: v for k, v in zip(cells, range(n))}

    mx = np.zeros((n, n_features), dtype=int)

    # Check if chromosome present in the fragments file
    if chromosome not in fragments.contigs:
        raise ValueError(f"Chromosome {chromosome} is not present in fragments file chromosomes: {fragments.contigs}")

    # logging.info(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Counting fragments in {n} cells for {features.shape[0]} features...")

    # Actually fetch the fragments
    _region_pileup(mx, fragments, d, chromosome, start, end)

    fragments.close()

    anno = pd.DataFrame(
        {"position": range(start, end)},
    )
    anno.index = anno.index.astype(str)

    return AnnData(X=mx, obs=pd.DataFrame(index=cells), var=anno, dtype=int)

def _tss_pileup(
    adata: AnnData,
    features: pd.DataFrame,
    extend_upstream: int = 1000,
    extend_downstream: int = 1000,
) -> AnnData:
    """
    Pile up reads in TSS regions. Returns a cell x position matrix that can be used for QC.

    Parameters
    ----------
    data
        AnnData object with associated fragments file.
    features
        A DataFrame with feature annotation, e.g. genes.
        Annotation has to contain columns: Chromosome, Start, End.
    extend_upsteam
        Number of nucleotides to extend every gene upstream (2000 by default to extend gene coordinates to promoter regions)
    extend_downstream
        Number of nucleotides to extend every gene downstream (0 by default)
    """
    if "files" not in adata.uns or "fragments" not in adata.uns["files"]:
        raise KeyError(
            "There is no fragments file located yet. Run muon.atac.tl.locate_fragments first."
        )

    pysam = import_pysam()

    n = adata.n_obs
    n_features = extend_downstream + extend_upstream + 1
    # Not sparse since we expect most positions to be filled
    mx = np.zeros((n, n_features), dtype=int)

    # Dictionary with matrix positions
    d = {k: v for k, v in zip(adata.obs.index, range(n))}

    fragments = locate_fragments(adata, return_fragments=True)

    # Subset the features to the chromosomes present in the fragments file
    chromosomes = fragments.contigs
    features = features[features.Chromosome.isin(chromosomes)]

    # logging.info(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Counting fragments in {n} cells for {features.shape[0]} features...")

    for i in tqdm(
        range(features.shape[0]), desc="Fetching Regions..."
    ):  # iterate over features (e.g. genes)

        f = features.iloc[i]
        tss_start = f.Start - extend_upstream  # First position of the TSS region
        tss_end = f.Start + extend_downstream  # Last position of the TSS region

        # Actually fetch the fragments
        _region_pileup(mx, fragments, d, f.Chromosome, tss_start, tss_end)

    fragments.close()

    anno = pd.DataFrame(
        {"TSS_position": range(-extend_upstream, extend_downstream + 1)},
    )
    anno.index = anno.index.astype(str)

    return AnnData(X=mx, obs=adata.obs, var=anno, dtype=int)


def fetch_regions_to_df(
    fragment_path: str,
    features: Union[pd.DataFrame, str],
    extend_upstream: int = 0,
    extend_downstream: int = 0,
    relative_coordinates=False,
) -> pd.DataFrame:
    """
    Parse peak annotation file and return it as DataFrame.

    Parameters
    ----------
    fragment_path
        Location of the fragments file (must be tabix indexed).
    features
        A DataFrame with feature annotation, e.g. genes or a string of format `chr1:1-2000000` or`chr1-1-2000000`.
        Annotation has to contain columns: Chromosome, Start, End.
    extend_upsteam
        Number of nucleotides to extend every gene upstream (2000 by default to extend gene coordinates to promoter regions)
    extend_downstream
        Number of nucleotides to extend every gene downstream (0 by default)
    relative_coordinates
        Return the coordinates with their relative position to the middle of the features.
    """

    if isinstance(features, str):
        features = atacutils.parse_region_string(features)
    n_features = features.shape[0]

    fragments = open_fragment_connection(fragment_path)

    dfs = []
    for i in tqdm(
        range(n_features), desc="Fetching Regions..."
    ):  # iterate over features (e.g. genes)
        f = features.iloc[i]
        try:
            fr = fragments.fetch(f.Chromosome, f.Start - extend_upstream, f.End + extend_downstream)
            df = pd.DataFrame(
                [(x.contig, x.start, x.end, x.name, x.score) for x in fr],
                columns=["Chromosome", "Start", "End", "Cell", "Score"],
            )
            if df.shape[0] != 0:
                df["Feature"] = f.Chromosome + "_" + str(f.Start) + "_" + str(f.End)

                if relative_coordinates:
                    middle = int(f.Start + (f.End - f.Start) / 2)
                    df.Start = df.Start - middle
                    df.End = df.End - middle

                dfs.append(df)
        except ValueError as e:
            # TODO this mostly happens when the chromosome is not present
            # could add explicit check
            print(e)
            print("Skipping this region...")

    fragments.close()

    df = pd.concat(dfs, axis=0, ignore_index=True)
    return df
