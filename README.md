# Uutiskooste

Hakee suomalaisten medioiden uutisotsikot RSS-syötteistä, niputtaa saman uutisen eri lähteistä ja kirjoittaa jokaisesta asiallisen otsikon ja lyhyen tiivistelmän Gemini-API:n ilmaistasolla. GitHub ajaa skriptin kolmesti päivässä ja julkaisee koosteen GitHub Pagesiin.

## Käyttöönotto

1. **Gemini-avain:** luo avain osoitteessa https://aistudio.google.com/apikey
2. **Repo:** luo GitHubissa uusi **julkinen** repo (ilmaisessa GitHub-tilissä Pages toimii vain julkisissa repoissa) ja lisää siihen nämä tiedostot:
   - `uutiskooste.py`
   - `.github/workflows/uutiskooste.yml`
3. **Avain salaisuudeksi:** Settings → Secrets and variables → Actions → *New repository secret*, nimi `GEMINI_API_KEY`, arvoksi avain.
4. **Pages päälle:** Settings → P
