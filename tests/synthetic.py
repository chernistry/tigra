"""A synthetic LinkedIn data export: invented people and companies, for tests, demos and screenshots.

    python tests/synthetic.py out/export.zip          # a ZIP, like the one LinkedIn sends
    python tests/synthetic.py out/export-dir --folder # the same files, unpacked

The output depends only on --seed and --people, so the same arguments give the same bytes.
Every name is made up from word parts; any likeness to a real person or company is chance.
"""

import argparse
import csv
import io
import random
import zipfile
from datetime import date, timedelta
from pathlib import Path

FIRST = [
    "Ari",
    "Noa",
    "Eli",
    "Maya",
    "Tal",
    "Yoni",
    "Dana",
    "Omer",
    "Lior",
    "Shai",
    "Gal",
    "Ron",
    "Mia",
    "Ben",
    "Zoe",
    "Leo",
    "Ivy",
    "Max",
    "Ada",
    "Kai",
    "Nina",
    "Sam",
    "Lena",
    "Theo",
    "Iris",
    "Jonah",
    "Ella",
    "Ravi",
]
SURNAME_HEAD = [
    "Al",
    "Bren",
    "Cor",
    "Dal",
    "Ev",
    "Fen",
    "Gar",
    "Hal",
    "Ior",
    "Jas",
    "Kel",
    "Lor",
    "Mar",
    "Nor",
    "Or",
    "Per",
    "Quin",
    "Ros",
    "Sel",
    "Tor",
    "Ul",
    "Ver",
    "Wyn",
    "Yar",
]
SURNAME_TAIL = ["dane", "wick", "mere", "ford", "holt", "ley", "stone", "by", "ton", "ridge", "vale", "more"]
COMPANY_HEAD = [
    "Amber",
    "Copper",
    "Quiet",
    "Velvet",
    "Paper",
    "Lucky",
    "Hollow",
    "Silver",
    "North",
    "Bright",
    "Cinder",
    "Maple",
    "Harbor",
    "Comet",
    "Thistle",
    "Willow",
]
COMPANY_TAIL = ["Finch", "Lantern", "Orchard", "Anchor", "Meadow", "Signal", "Beacon", "Kettle", "Ridge", "Loom"]
COMPANY_KIND = ["Labs", "Cloud", "Systems", "Works", "Data", "Robotics"]
GENERIC = ["Stealth Startup", "Self-employed", ""]
ROLES = [
    "Software Engineer",
    "Senior Software Engineer",
    "ML Engineer",
    "Data Scientist",
    "Founder & CEO",
    "Co-Founder & CTO",
    "VP Engineering",
    "Technical Recruiter",
    "Talent Acquisition Partner",
    "DevOps Engineer",
    "SRE",
    "Product Manager",
    "Security Researcher",
    "Head of AI",
    "General Partner",
    "Angel Investor",
    "Platform Engineer",
    "Engineering Manager",
    "Developer Advocate",
    "Account Executive",
    "Product Designer",
    "Research Scientist, evaluation infrastructure for long-running agents",
    "Student",
    "",
]
OWNER = ("Sample Owner", "https://www.linkedin.com/in/sample-owner")
AS_OF = date(2026, 11, 10)
START = date(2023, 1, 1)


def _csv(rows: list[list[str]], preamble: str = "", quote_all: bool = False) -> bytes:
    buf = io.StringIO()
    buf.write(preamble)
    csv.writer(buf, quoting=csv.QUOTE_ALL if quote_all else csv.QUOTE_MINIMAL, lineterminator="\n").writerows(rows)
    return buf.getvalue().encode("utf-8")


def generate(seed: int = 7, people: int = 320) -> dict[str, bytes]:
    """File name -> content of a synthetic export."""
    rnd = random.Random(seed)
    companies = sorted({f"{h} {t} {k}" for h in COMPANY_HEAD for t in COMPANY_TAIL for k in COMPANY_KIND})
    companies = rnd.sample(companies, 28) + GENERIC
    weights = [1 / (i + 1) ** 0.9 for i in range(len(companies))]
    surnames = [h + t for h in SURNAME_HEAD for t in SURNAME_TAIL]
    rnd.shuffle(surnames)

    contacts, rows = [], [["First Name", "Last Name", "URL", "Email Address", "Company", "Position", "Connected On"]]
    for i in range(people):
        first, last = rnd.choice(FIRST), surnames[i % len(surnames)]
        slug = f"{first}-{last}-{i:03d}".lower()
        days = int((1 - rnd.random() ** 2.5) * (AS_OF - START).days)
        connected = START + timedelta(days=days)
        if i < 45:  # a conference-style burst of new connections
            connected = date(2025, 9, 10) + timedelta(days=i % 3)
        rows.append(
            [
                first,
                last,
                f"https://www.linkedin.com/in/{slug}",
                "",
                rnd.choices(companies, weights)[0],
                rnd.choice(ROLES),
                connected.strftime("%d %b %Y"),
            ]
        )
        contacts.append((f"{first} {last}", f"https://www.linkedin.com/in/{slug}"))
    rows.append(["Rina", "Orwick", "", "", 'חברה לדוגמה בע"מ', "מהנדסת תוכנה", "01 Feb 2026"])  # right-to-left text
    rows.append(["", "", "", "", "", "", ""])  # a deactivated member: nothing to identify
    files = {"Connections.csv": _csv(rows, 'Notes:\n"Synthetic export; every person here is invented."\n\n')}

    msgs = [
        [
            "CONVERSATION ID",
            "CONVERSATION TITLE",
            "FROM",
            "SENDER PROFILE URL",
            "TO",
            "RECIPIENT PROFILE URLS",
            "DATE",
            "SUBJECT",
            "CONTENT",
            "FOLDER",
            "ATTACHMENTS",
            "IS MESSAGE DRAFT",
        ]
    ]
    together = [date(2025, 2, 3) + timedelta(weeks=5 * w) for w in range(6)]  # four contacts, same weeks
    for idx, (name, url) in enumerate(rnd.sample(contacts, 70)):
        for k in range(12 if idx < 4 else rnd.choice([1, 1, 2, 3, 5, 8, 20])):
            day = (
                together[k % 6] + timedelta(days=k % 2)
                if idx < 4
                else date(2024, 1, 1) + timedelta(days=rnd.randint(0, 1040))
            )
            src, dst = (OWNER, (name, url)) if rnd.random() < 0.5 else ((name, url), OWNER)
            stamp = f"{day:%Y-%m-%d} {rnd.randint(7, 22):02d}:{rnd.randint(0, 59):02d}:00 UTC"
            msgs.append(
                [f"conv-{idx:03d}", "", src[0], src[1], dst[0], dst[1], stamp, "", "(synthetic)", "INBOX", "", "No"]
            )
    files["messages.csv"] = _csv(msgs, quote_all=True)

    inv = [["From", "To", "Sent At", "Message", "Direction", "inviterProfileUrl", "inviteeProfileUrl"]]
    for name, url in rnd.sample(contacts, 120):
        day = date(2025, 1, 1) + timedelta(days=rnd.randint(0, 670))
        note = rnd.choice(["", "", "", "Hi, we met at the meetup last week & talked about evals."])
        sent = f"{day.month}/{day.day}/{day:%y}, 9:15 AM"
        if rnd.random() < 0.7:
            inv.append([OWNER[0], name, sent, note, "OUTGOING", OWNER[1], url])
        else:
            inv.append([name, OWNER[0], sent, note, "INCOMING", url, OWNER[1]])
    files["Invitations.csv"] = _csv(inv)

    first_job, second_job = companies[0], companies[3]
    files["Positions.csv"] = _csv(
        [
            ["Company Name", "Title", "Description", "Location", "Started On", "Finished On"],
            [first_job, "Engineer", "", "", "Jul 2023", "Dec 2024"],
            [second_job, "Specialist", "", "", "Jan 2026", ""],
        ]
    )
    files["Company Follows.csv"] = _csv(
        [["Organization", "Followed On"], [companies[5], "Wed Nov 04 20:50:55 GMT 2026"]]
    )
    files["Profile.csv"] = _csv([["First Name", "Last Name", "Headline"], ["Sample", "Owner", "Builds things"]])
    return files


def write(files: dict[str, bytes], out: Path, folder: bool = False) -> Path:
    if folder:
        out.mkdir(parents=True, exist_ok=True)
        for name, data in files.items():
            (out / name).write_bytes(data)
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name in sorted(files):  # fixed order and timestamps: byte-identical archives
            z.writestr(zipfile.ZipInfo(name, date_time=(2026, 11, 10, 0, 0, 0)), files[name], zipfile.ZIP_DEFLATED)
    return out


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("out", type=Path, help="Output ZIP path (or folder with --folder).")
    parser.add_argument("--folder", action="store_true", help="Write the unpacked files instead of a ZIP.")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--people", type=int, default=320)
    args = parser.parse_args(argv)
    print(write(generate(args.seed, args.people), args.out, args.folder))


if __name__ == "__main__":
    main()
