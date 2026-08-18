"""
Export the database back out to CSV or to a legacy-shaped JSON array.

The reverse of seed_roles: useful for backups, for handing HR a spreadsheet,
and for regenerating a static fallback copy of the page if the app is ever
taken offline.

    python manage.py export_roles --format csv  > roles.csv
    python manage.py export_roles --format json > roles.json
    python manage.py export_roles --format js   > roles.js   # `const ROLES = [...]`
"""

from __future__ import annotations

import csv
import io
import json

from django.core.management.base import BaseCommand

from roles.models import Role

COLUMNS = [
    ("bu", lambda r: r.business_unit.name),
    ("pos", lambda r: r.position),
    ("band", lambda r: r.band),
    ("bandN", lambda r: float(r.band_numeric) if r.band_numeric is not None else None),
    ("level", lambda r: r.level),
    ("exp", lambda r: r.experience),
    ("quals", lambda r: r.qualifications),
    ("desc", lambda r: r.purpose),
    ("focus", lambda r: r.focus_areas),
    ("kras", lambda r: r.kras),
    ("reports", lambda r: r.direct_reports),
    ("techcomp", lambda r: r.technical_competencies),
    ("leadcomp", lambda r: r.leadership_competencies),
]


class Command(BaseCommand):
    help = "Export roles as CSV, JSON, or a legacy `const ROLES = [...]` JavaScript array."

    def add_arguments(self, parser):
        parser.add_argument(
            "--format",
            choices=("csv", "json", "js"),
            default="csv",
            help="Output format (default: csv).",
        )
        parser.add_argument(
            "--include-inactive",
            action="store_true",
            help="Include roles that are hidden from the public site.",
        )
        parser.add_argument(
            "--output",
            default="-",
            help="File to write to. '-' (default) writes to stdout.",
        )

    def handle(self, *args, **options):
        queryset = Role.objects.with_bu().order_by(
            "business_unit__display_order", "band_numeric", "position"
        )
        if not options["include_inactive"]:
            queryset = queryset.active()

        rows = [{key: getter(role) for key, getter in COLUMNS} for role in queryset]

        # Render into a buffer first, then emit in one write.
        # self.stdout is Django's OutputWrapper, which appends a newline to
        # every write() call — streaming JSON or CSV through it directly
        # inserts line breaks mid-record.
        buffer = io.StringIO()
        if options["format"] == "csv":
            writer = csv.DictWriter(buffer, fieldnames=[key for key, _ in COLUMNS])
            writer.writeheader()
            writer.writerows(rows)
        elif options["format"] == "json":
            json.dump(rows, buffer, indent=2, ensure_ascii=False)
            buffer.write("\n")
        else:
            buffer.write("const ROLES = [\n")
            for row in rows:
                buffer.write("  " + json.dumps(row, ensure_ascii=False) + ",\n")
            buffer.write("];\n")

        text = buffer.getvalue()
        target = options["output"]
        if target == "-":
            self.stdout.write(text, ending="")
        else:
            with open(target, "w", encoding="utf-8", newline="") as handle:
                handle.write(text)

        self.stderr.write(
            self.style.SUCCESS(f"export_roles: wrote {len(rows)} role(s) as {options['format']}.")
        )
