---
name: "title-discovery"
version: "1.0.0"
description: "Find where a movie or series streams in India (Netflix, Prime Video subscription)."
tools: ["search_title"]
max_turns: 4
keywords: ["movie", "movies", "film", "films", "series", "show", "shows", "season", "episode", "netflix", "prime", "stream", "streaming", "watch"]
input: "discovery/1#params_discover"
output: "discovery/1#data_discover.matches"
---
To find where a movie or series streams, call search_title with the title. Add a year only if the
question gives one: a guessed year makes the search miss. If the question names several titles, search
for each. If a search finds several titles with the same name, report them all with their years.
