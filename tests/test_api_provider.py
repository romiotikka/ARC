from scripts.identity.providers.api_sports import ApiSportsProvider


provider = ApiSportsProvider()

print("=" * 50)
print("SEARCH")
print("=" * 50)

players = provider.search_players("Kitsing")

print(f"Found {len(players)} players\n")

for player in players:
    print(player)


print("\n" + "=" * 50)
print("TEAMS")
print("=" * 50)

teams = provider.get_teams(
    league_id=204,
    season=2025
)

print(f"Found {len(teams)} teams\n")

for team in teams[:5]:
    print(team)


print("\n" + "=" * 50)
print("ROSTER")
print("=" * 50)

roster = provider.get_season_roster(
    team_id=480,
    season=2025
)

print(f"Roster size: {len(roster)}\n")

for player in roster:
    print(player)