import argparse
import re
import sys
from pathlib import Path
import pandas as pd

# Change to "{prefix}{n}.wav" if you don't want the underscore,
# or use "{prefix}_{n:03d}.wav" for zero padding (HFC_001.wav).
NAME_FORMAT = "{prefix}_{n}.wav"

ap = argparse.ArgumentParser(
    description="Merge every output.xlsx under a root folder into one Excel "
                "file, written inside that same root folder."
)
ap.add_argument("-r", "--root", type=Path, default=Path.cwd(),
                help="root folder to scan (default: current folder)")
ap.add_argument("-o", "--out", default="merged.xlsx",
                help="name of the merged file, created inside the root "
                     "(default: merged.xlsx)")
args = ap.parse_args()

root = args.root.expanduser().resolve()
if not root.is_dir():
    sys.exit(f"Not a folder: {root}")

out = root / args.out          # always written inside the root folder
print(f"Scanning {root}\n")

files = sorted(root.rglob("output.xlsx"))
if not files:
    sys.exit(f"No output.xlsx found under {root}")

frames = []
headers = {}

for f in files:
    df = pd.read_excel(f)
    headers[f] = list(df.columns)
    df.columns = range(df.shape[1])
    frames.append(df)
    print(f.relative_to(root), len(df), f"({df.shape[1]} cols)")

# The widest file defines the final column layout.
ncols = max(len(c) for c in headers.values())
widest = next(f for f in files if len(headers[f]) == ncols)
final_cols = headers[widest]
print(f"\nUsing {ncols} columns, header taken from {widest.relative_to(root)}")

# Report where a file's column name disagrees with the reference at the same
# position, so you can check that positional filling did the right thing.
for f, cols in headers.items():
    for i, name in enumerate(cols):
        ref = final_cols[i]
        if str(name).startswith("Unnamed:") or str(ref).startswith("Unnamed:"):
            print(f"  CHECK {f.relative_to(root)}: position {i} is '{name}', reference is '{ref}'")
        elif name != ref:
            print(f"  WARN  {f.relative_to(root)}: position {i} is '{name}', reference is '{ref}'")

# Missing trailing columns become empty cells.
frames = [df.reindex(columns=range(ncols)) for df in frames]

merged = pd.concat(frames, ignore_index=True)
merged.columns = final_cols

# --- Global renumbering of the first column -------------------------------
# Rows keep the order in which they were read (folder by folder, then row by
# row), so the counter per prefix simply continues across folders.
name_col = merged.columns[0]
counters = {}
new_names = []
unmatched = []

for i, value in enumerate(merged[name_col]):
    m = re.search(r"(HFC|LFC)", str(value), flags=re.IGNORECASE)
    if not m:
        unmatched.append((i, value))
        new_names.append(value)
        continue
    prefix = m.group(1).upper()
    counters[prefix] = counters.get(prefix, 0) + 1
    new_names.append(NAME_FORMAT.format(prefix=prefix, n=counters[prefix]))

merged[name_col] = new_names

for prefix, count in sorted(counters.items()):
    print(f"\nRenumbered {count} {prefix} rows (1 to {count})")
if unmatched:
    print(f"\n{len(unmatched)} row(s) had no HFC/LFC in the name, left unchanged:")
    for i, value in unmatched[:10]:
        print(f"  row {i + 2}: {value!r}")
    if len(unmatched) > 10:
        print(f"  ... and {len(unmatched) - 10} more")

merged.to_excel(out, index=False)
print("\n->", out, len(merged), "rows x", merged.shape[1], "cols")