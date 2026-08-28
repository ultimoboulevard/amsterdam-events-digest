import httpx
from bs4 import BeautifulSoup

resp = httpx.get("https://www.splendoramsterdam.com/agenda")
soup = BeautifulSoup(resp.text, "html.parser")
for a in soup.select("a.text"):
    print("PARENT:", a.parent.name, a.parent.get("class"))
    print("HTML:", a.parent.prettify()[:1000])
    break
