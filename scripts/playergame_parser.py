import requests


def parse_playergames(data, match_id):

    player_games = []

    for team_no, team in data["tm"].items():

        team_name = team["name"]

        for player_key, player in team["pl"].items():

            player_game = {

                "game_id": match_id,

                "player_name": player["name"],
                "team_name": team_name,

                "shirt_number": player["shirtNumber"],
                "position": player["playingPosition"],

                "minutes": player["sMinutes"],

                "points": player["sPoints"],

                "off_reb": player["sReboundsOffensive"],
                "def_reb": player["sReboundsDefensive"],
                "tot_reb": player["sReboundsTotal"],

                "assists": player["sAssists"],
                "steals": player["sSteals"],
                "blocks": player["sBlocks"],
                "turnovers": player["sTurnovers"],

                "fgm": player["sFieldGoalsMade"],
                "fga": player["sFieldGoalsAttempted"],

                "tpm": player["sThreePointersMade"],
                "tpa": player["sThreePointersAttempted"],

                "ftm": player["sFreeThrowsMade"],
                "fta": player["sFreeThrowsAttempted"],

                "plus_minus": player["sPlusMinusPoints"],

                "starter": player["starter"]
            }

            player_games.append(player_game)

    return player_games


# --------------------
# TEST
# --------------------

match_id = "2836380"

url = f"https://fibalivestats.dcd.shared.geniussports.com/data/{match_id}/data.json"

data = requests.get(url).json()

player_games = parse_playergames(data, match_id)

print(f"Leitud mängijaid: {len(player_games)}")
print()

print(player_games[0])