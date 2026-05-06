import httpx

url = "https://ra.co/graphql"
headers = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Content-Type": "application/json",
    "Origin": "https://ra.co",
    "Referer": "https://ra.co/events/nl/amsterdam",
}

query = """
query {
  eventListings(filters: { areas: { eq: 29 } }, pageSize: 5) {
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
r = httpx.post(url, json={"query": query}, headers=headers)
print(r.status_code)
print(r.text)
