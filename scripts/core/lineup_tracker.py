import requests

url = "https://fibalivestats.dcd.shared.geniussports.com/data/2836380/data.json"

data = requests.get(url).json()

for team_no in ["1", "2"]:

    team = data["tm"][team_no]

    print()
    print(team["name"])
    print("-" * 40)

    starters = []

    for player_id, player in team["pl"].items():

        if player["starter"] == 1:

            starters.append(player["name"])

    for player in starters:
        print(player)