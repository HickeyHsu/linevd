import pandas as pd
import sastvd as svd
import sastvd.helpers.git as svdg
from sklearn.model_selection import train_test_split
from tqdm import tqdm

tqdm.pandas()


def train_val_test_split_df(df, idcol, stratifycol):
    """Add train/val/test column into dataframe."""
    X = df[idcol]
    y = df[stratifycol]
    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.2, random_state=1
    )
    X_train, X_val, y_train, y_val = train_test_split(
        X_train, y_train, test_size=0.25, random_state=1
    )
    X_train = set(X_train)
    X_val = set(X_val)
    X_test = set(X_test)

    def path_to_label(path):
        if path in X_train:
            return "train"
        if path in X_val:
            return "val"
        if path in X_test:
            return "test"

    df["label"] = df[idcol].apply(path_to_label)
    return df


def bigvul(minimal=True):
    """Read BigVul Data."""
    savedir = svd.get_dir(svd.cache_dir() / "minimal_datasets")
    if minimal:
        try:
            return pd.read_csv(savedir / "minimal_bigvul.csv").dropna()
        except:
            pass
    df = pd.read_csv(svd.external_dir() / "MSR_data_cleaned.csv")
    df = df.rename(columns={"Unnamed: 0": "id"})
    df["dataset"] = "bigvul"
    svdg.mp_code2diff(df)
    df = train_val_test_split_df(df, "id", "vul")
    df["added"] = df.progress_apply(svdg.allfunc, comment="added", axis=1)
    df["removed"] = df.progress_apply(svdg.allfunc, comment="removed", axis=1)
    df["diff"] = df.progress_apply(svdg.allfunc, comment="diff", axis=1)
    df["before"] = df.progress_apply(svdg.allfunc, comment="before", axis=1)
    df["after"] = df.progress_apply(svdg.allfunc, comment="after", axis=1)
    keepcols = ["dataset", "id", "label", "removed", "added", "diff", "before", "after"]
    df[keepcols].to_csv(savedir / "minimal_bigvul.csv", index=0)
    return df
