import gzip
import json
import zipfile
from argparse import ArgumentParser
from pathlib import Path

import numpy as np
import pandas as pd

from common_lib import get_paths, timer, transform, write
from sf_transformers import TRANSFORMERS


def preprocess_path(in_path, out_path, nfiles=None):
    data = read_raw(in_path, nfiles)
    df_cvr, df_contest, df_candidate, df_office, df_mark = preprocess(*data)
    write(df_cvr, df_contest, df_candidate, df_office, df_mark, out_path)


@timer
def read_raw(path, nfiles):
    path = list(path.glob("*.gz"))[0]
    with gzip.open(path) as gz_file:
        with zipfile.ZipFile(gz_file, "r") as zip_file:
            with zip_file.open("ContestManifest.json") as contest_manifest:
                contest_data = json.load(contest_manifest)

            with zip_file.open("CandidateManifest.json") as candidate_manifest:
                candidate_data = json.load(candidate_manifest)

            mark_data = []
            cvr_id = 0
            cvr_data = []
            total = len(zip_file.filelist)
            for idx, zip_info in enumerate(zip_file.filelist):
                if idx == nfiles:
                    break

                if idx % 1000 == 0:
                    print(idx, "of", total)

                if not zip_info.filename.startswith("CvrExport_"):
                    continue

                with zip_file.open(zip_info) as cvr_export:
                    data = json.load(cvr_export)

                for sess in data["Sessions"]:
                    orig = sess["Original"]
                    for card in orig["Cards"]:
                        cvr_data.append([cvr_id, zip_info.filename])
                        for contest in card["Contests"]:
                            contest_id = contest["Id"]
                            for mark in contest["Marks"]:
                                data = [
                                    cvr_id,
                                    contest_id,
                                    mark["CandidateId"],
                                    mark["Rank"],
                                    mark["IsVote"],
                                    mark["IsAmbiguous"],
                                ]
                                mark_data.append(data)
                        cvr_id += 1
    return cvr_data, contest_data, candidate_data, mark_data


@timer
def preprocess(cvr_data, contest_data, candidate_data, mark_data):
    df_cvr = pd.DataFrame(cvr_data, columns=["cvr_id", "filename"]).set_index("cvr_id")
    df_cvr.index = df_cvr.index.astype(np.int32)
    df_cvr = df_cvr.astype("category")

    df_contest = pd.json_normalize(contest_data, record_path=["List"])
    contest_dtypes = {
        "Id": np.int16,
        "ExternalId": np.int32,
        "DistrictId": np.int16,
        "VoteFor": np.int8,
        "NumOfRanks": np.int8,
        "Disabled": np.bool_,
    }
    df_contest = df_contest.astype(contest_dtypes)
    df_contest = df_contest.rename(columns={"Id": "contest_id", "VoteFor": "vote_for"})

    df_contest["ranked"] = df_contest["NumOfRanks"] > 1
    is_ranked = df_contest["ranked"]
    df_contest.loc[is_ranked, "vote_for"] = df_contest.loc[is_ranked, "NumOfRanks"]
    df_contest = df_contest.set_index("contest_id").sort_index()

    df_contest = (
        df_contest.apply(standardize, axis="columns")
        .join(df_contest)
        .drop(columns="Description")
    )
    office_levels = ["level", "jurisdiction", "office", "district"]
    oid = df_contest.set_index(office_levels).index.factorize()[0].astype(np.int16)
    df_contest["office_id"] = oid

    df_office = df_contest[office_levels + ["office_id"]].drop_duplicates()
    df_office = df_office.set_index("office_id").astype("category")

    df_contest = df_contest.drop(columns=office_levels)

    df_candidate = pd.json_normalize(candidate_data, record_path=["List"])
    df_candidate["party"] = None
    cand_dtypes = {
        "Id": np.int16,
        "ExternalId": "category",
        "ContestId": np.int16,
        "Type": "category",
        "Disabled": np.bool_,
        "party": "category",
    }
    df_candidate = df_candidate.astype(cand_dtypes)
    df_candidate = df_candidate.rename(
        columns={"Id": "candidate_id", "Description": "name", "ContestId": "contest_id"}
    )
    df_candidate = df_candidate.set_index("candidate_id").sort_index()

    mark_dtypes = {
        "cvr_id": np.int32,
        "contest_id": np.int16,
        "candidate_id": np.int16,
        "rank": np.int8,
        "is_vote": np.bool_,
        "is_ambiguous": np.bool_,
    }
    df_mark = pd.DataFrame(mark_data, columns=mark_dtypes.keys())
    df_mark = df_mark.astype(mark_dtypes)
    df_mark = df_mark.set_index(["cvr_id", "contest_id", "rank"]).sort_index()

    return df_cvr, df_contest, df_office, df_candidate, df_mark


def standardize(row):
    values = transform(row["Description"], TRANSFORMERS)
    return pd.Series(values, index=["level", "jurisdiction", "office", "district"])


if __name__ == "__main__":
    parser = ArgumentParser(description="Standardizes San Francisco cast vote records")
    parser.add_argument(
        "path", type=Path, help="path of data: year/state/level/jurisdiction/election"
    )
    parser.add_argument("--nfiles", type=int, help="# files to process")
    args = parser.parse_args()
    in_path, out_path = get_paths(args.path)
    preprocess_path(in_path, out_path, args.nfiles)