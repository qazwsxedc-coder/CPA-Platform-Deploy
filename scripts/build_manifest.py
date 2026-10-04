#!/usr/bin/env python3
import argparse, datetime, hashlib, json
from pathlib import Path

def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

def main():
    parser = argparse.ArgumentParser()
    for name in ("previous", "output", "cli-version", "cli-image-id", "cli-digest", "cli-source-commit",
                 "manager-version", "manager-image-id", "manager-digest", "manager-source-commit",
                 "cli-from", "manager-from"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    previous = json.loads(Path(args.previous).read_text())
    releases = list(previous.get("releases", []))
    existing = {r["releaseId"]: r for r in releases}
    now = datetime.datetime.utcnow().isoformat() + "Z"

    def make(component, version, image_id, image_digest, source_commit, allowed, source):
        evidence = {"startupPassed": True, "apiSmokePassed": True, "imageId": image_id,
                    "allowedFromImageIds": [allowed], "officialCompatible": True,
                    "recognizedAdditive": False, "migrationRehearsalPassed": False,
                    "validatedAt": now}
        registry = "ghcr.io/qazwsxedc-coder/" + ("cpa-cli" if component == "cli" else "cpa-manager")
        item = {"releaseId": component + "-" + version, "component": component, "version": version,
                "imageTag": registry + ":" + version, "imageSource": source,
                "imageDigest": image_digest, "imageId": image_id, "sourceCommit": source_commit,
                "allowedFromImageIds": [allowed], "rollbackDataCompatible": True,
                "migrationRequired": False, "migrationMode": "none", "evidence": evidence,
                "evidenceSha256": digest(evidence), "validatedAt": now}
        if item["releaseId"] in existing:
            if any(existing[item["releaseId"]].get(key) != item.get(key)
                   for key in ("component", "version", "imageTag", "imageDigest", "imageId",
                               "sourceCommit", "allowedFromImageIds", "migrationMode")):
                raise SystemExit("published_candidate_changed")
            return existing[item["releaseId"]]
        else:
            releases.append(item)
        return item

    cli = make("cli", args.cli_version, args.cli_image_id, args.cli_digest, args.cli_source_commit, args.cli_from, "official")
    manager = make("manager", args.manager_version, args.manager_image_id, args.manager_digest, args.manager_source_commit, args.manager_from, "custom")
    output = {"schemaVersion": 1, "channel": "stable", "generatedAt": now,
              "currentBaseline": {"cli": cli["imageId"], "manager": manager["imageId"]},
              "latest": {"cli": args.cli_version, "manager": args.manager_version},
              "releases": releases, "preparation": {"state": "published", "reason": "", "source": "GitHub Actions"}}
    Path(args.output).write_text(json.dumps(output, ensure_ascii=False, sort_keys=True, indent=2) + "\n")

if __name__ == "__main__":
    main()
