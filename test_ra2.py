import httpx
from datetime import datetime

url = "https://ra.co/graphql"
headers = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Content-Type": "application/json",
    "Origin": "https://ra.co",
}

today = datetime.now().strftime("%Y-%m-%dT00:00:00.000Z")

query = """
query GET_EVENTS($filters: FilterInputDtoInput) {
  eventListings(filters: $filters, pageSize: 5) {
    data {
      event {
        id
        title
        date
        venue { name }
      }
    }
  }
}
"""

variables = {
    "filters": {
        "areas": {"eq": 29},
        "listingDate": {"gte": today}
    }
}

r = httpx.post(url, json={"query": query, "variables": variables}, headers=headers)
print(r.text)
