import requests
import re
import csv

url = "https://www.estlatbl.com/et/tulemused?setSid=2026"

response = requests.get(url)

html = response.text

game_ids = sorted(
    set(
        re.findall(
            r"/et/tulemused/(\d+)/#c",
            html
        )
    )
)

print("Leitud mänge:", len(game_ids))

success = 0

with open(
    "estlatbl_2026_games.csv",
    "w",
    newline="",
    encoding="utf-8"
) as file:

    writer = csv.writer(file)

    writer.writerow([
        "game_id",
        "match_id",
        "json_url"
    ])

    for game_id in game_ids:

        live_stats_url = (
            f"https://www.estlatbl.com/et/tulemused/{game_id}/live-stats"
        )

        response = requests.get(live_stats_url)

        match = re.search(
            r"matchId=(\d+)",
            response.text
        )

        if match:

            match_id = match.group(1)

            json_url = (
                f"https://fibalivestats.dcd.shared.geniussports.com/data/"
                f"{match_id}/data.json"
            )

            writer.writerow([
                game_id,
                match_id,
                json_url
            ])

            success += 1

print()
print("CSV fail loodud.")
print("Leitud:", success)
print("Kokku:", len(game_ids))