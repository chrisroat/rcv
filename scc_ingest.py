import gzip
import re
from argparse import ArgumentParser
from pathlib import Path

import numpy as np
import pandas as pd

from common_lib import get_paths, timer, transform, write
from scc_transformers import TRANSFORMERS


def preprocess_path(in_path, out_path, nrows=None):
    df = read_raw(in_path, nrows=nrows)
    df_cvr, df_contest, df_office, df_candidate, df_mark = preprocess(df)
    write(df_cvr, df_contest, df_office, df_candidate, df_mark, out_path)


@timer
def read_raw(path, nrows=None):
    path = list(path.glob("*.gz"))[0]
    with gzip.open(path) as gz_file:
        df = pd.read_csv(gz_file, header=[1, 2, 3], na_values=[0], nrows=nrows)
    print(f"Raw data total rows: {df.shape[0]}")
    return df


@timer
def preprocess(df):
    reformat_strings(df)
    set_index(df)

    df_cvr = split_cvr(df)
    check_and_clean_id_invariants(df_cvr)

    update_column_index(df)
    df = tidy(df)

    df_contest, df_office = split_contest(df)
    df_candidate = split_candidate(df)

    df.reset_index(level="candidate_id", inplace=True)
    df.set_index("rank", append=True, inplace=True)
    df.sort_index(inplace=True)

    return df_cvr, df_contest, df_office, df_candidate, df


def reformat_strings(df):
    reformat_cols = df.columns[:5]
    for col in reformat_cols:
        df[col] = df[col].str[2:-1]


def set_index(df):
    index_col = df.columns[0]
    df[index_col] = df[index_col].astype(np.int32)
    df.set_index(index_col, inplace=True)
    df.index.name = "cvr_id"


def split_cvr(df):
    id_cols = df.columns[:7]
    df_cvr = df[id_cols]
    df_cvr = df_cvr.droplevel([0, 1], axis="columns")
    df.drop(columns=id_cols, inplace=True)
    return df_cvr


def check_and_clean_id_invariants(df_id):
    i_id = df_id["TabulatorNum"] + "-" + df_id["BatchId"] + "-" + df_id["RecordId"]
    imprinted_id_ok = (df_id["ImprintedId"] == i_id) | (df_id["ImprintedId"] == "")
    assert imprinted_id_ok.all(), "ImprintedId mismatch"

    df_id.drop(columns="ImprintedId", inplace=True)
    cols = ["TabulatorNum", "BatchId"]
    df_id[cols] = df_id[cols].astype(np.int16)

    df_check = df_id["BallotType"].str.extract(r"(?P<a>.+) \((?P<b>.+)\)")
    ballot_type_ok = df_check["a"] == df_check["b"]
    assert ballot_type_ok.all(), "BallotType mismatch"

    df_id["BallotType"] = df_check["a"].astype("category")

    precinct_regex = r"0*(?P<a>\d+) \((?P<Precinct1>\d+)-?(?P<Precinct2>\d*)\)"
    df_check = df_id["PrecinctPortion"].str.extract(precinct_regex)

    precinct_ok = df_check["a"] == df_check["Precinct1"]
    assert precinct_ok.all(), "PrecintPortion mismatch"

    df_id.drop(columns="PrecinctPortion", inplace=True)
    precints = ["Precinct1", "Precinct2"]
    df_id[precints] = df_check[precints].astype("category")

    df_id["RecordId"] = df_id["RecordId"].astype(np.int32)
    df_id["CountingGroup"] = df_id["CountingGroup"].astype("category")


def update_column_index(df):
    # - Change "Unnamed: *" levels to ""
    # - Change bond response to plain YES/NO
    # - Level values as categorical
    # - Level names to be descriptive
    # - Merge candidate and party into single level (using ';' delimiter)

    # Drop write-ins from data (it makes column multi-index non-unique)
    df.drop(columns="Write-in", level=1, inplace=True)

    df_cols = (
        df.columns.to_frame()
        .reset_index(drop=True)
        .apply(lambda x: np.where(x.str.contains("Unnamed"), "", x))
    )
    df_cols.columns = ["contest", "candidate", "party"]
    df_cols["candidate"] = df_cols["candidate"].replace(
        r"BONDS—(YES|NO)", r"\1", regex=True
    )
    df_cols["candidate"] = df_cols["candidate"] + ";" + df_cols["party"]
    df_cols = df_cols.drop(columns="party")
    df_cols = df_cols.astype("category")
    df.columns = pd.MultiIndex.from_frame(df_cols)


def tidy(df):
    df = df.melt(value_name="rank", ignore_index=False).dropna()
    df["rank"] = df["rank"].astype(np.int8)
    df.set_index(["contest", "candidate"], append=True, inplace=True)
    df.sort_index(inplace=True)
    df.name = "mark"
    return df


def split_contest(df):
    contest_level = df.index.names.index("contest")
    contest_data = [standardize(contest) for contest in df.index.levels[contest_level]]

    columns = [
        "level",
        "jurisdiction",
        "office",
        "district",
        "term",
        "vote_for",
    ]

    df_contest = pd.DataFrame(contest_data, columns=columns)
    df_contest.index = df_contest.index.astype(np.int16)
    df_contest.index.name = "contest_id"
    df_contest["term"] = df_contest["term"].astype("category")
    df_contest["vote_for"] = df_contest["vote_for"].astype(np.int8)
    df_contest["ranked"] = False

    office_levels = ["level", "jurisdiction", "office", "district"]
    oid = df_contest.set_index(office_levels).index.factorize()[0].astype(np.int16)
    df_contest["office_id"] = oid

    df_office = df_contest[office_levels + ["office_id"]].drop_duplicates()
    df_office = df_office.set_index("office_id").astype("category")

    df_contest = df_contest.drop(columns=office_levels)
    df.index = df.index.set_levels(df_contest.index, level="contest")
    df.index = df.index.set_names("contest_id", level="contest")
    return df_contest, df_office


def standardize(contest):
    contest = contest.replace("  ", " ")

    match = re.search(r" \(Vote For=(\d+)\)$", contest)
    vote_for = int(match.group(1))
    contest = contest[: match.start()]

    match = re.search(r", ([^,]*) Term$", contest)
    term = match.group(1) if match else "Full"
    if match:
        contest = contest[: match.start()]

    return transform(contest, TRANSFORMERS) + (term, vote_for)


def split_candidate(df):
    candidate_level = df.index.names.index("candidate")
    cand_data = [e.split(";") for e in df.index.levels[candidate_level]]
    df_candidate = pd.DataFrame(cand_data, columns=["name", "party"]).replace("", None)
    df_candidate["party"] = df_candidate["party"].astype("category")
    df_candidate.index.name = "candidate_id"
    df_candidate.index = df_candidate.index.astype(np.int16)
    df.index = df.index.set_levels(df_candidate.index, level="candidate")
    df.index = df.index.set_names("candidate_id", level="candidate")
    return df_candidate


if __name__ == "__main__":
    parser = ArgumentParser(
        description="Standardizes Santa Clara County cast vote records"
    )
    parser.add_argument(
        "path", type=Path, help="path of data: year/state/level/jurisdiction/election"
    )
    parser.add_argument("--nrows", type=int, default=None, help="# rows to process")
    args = parser.parse_args()
    in_path, out_path = get_paths(args.path)
    preprocess_path(in_path, out_path, args.nrows)