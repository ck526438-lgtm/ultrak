# Contribuer

## Installer l'environnement

```bash
git clone https://github.com/ck526438-lgtm/ultrak.git && cd ultrak
python -m pip install pytest
# ffmpeg est trouve tout seul (PATH, ~/.ultrak/bin, ou tools/ffmpeg.exe)
```

Aucune dependance runtime : la bibliothèque est Python 3.8+ et ffmpeg uniquement.

## Lancer les tests

```bash
python -m pytest -q                  # 35 tests unites + 8 tests d'integration ffmpeg
python tools/lint_filters.py ffmpeg  # valide CHAQUE filtre emis contre le binaire reel
```

`lint_filters.py` n'est pas decoratif : une option hors intervalle (ex. `deband=1:1:6:8`)
fait echouer 100 % des rendus sur la machine d'un utilisateur, et le lint l'attrape en
2 secondes. Toute nouvelle chaine de filtres doit passer par la.

## Regenerer les visuels du README

```bash
python tools/make_demo.py                                    # docs/demo/source-scene.jpg -> demo/source_720.mp4
python ultrak.py upscale demo/source_720.mp4 --yes           # rendu honnete
python ultrak.py compare <src> <dist> --gif --metrics        # poster, zoom, GIF, PSNR/SSIM
python tools/shot_gui.py docs/assets/gui.png <video>         # vraie capture de la fenetre
```

Les chiffres du README viennent de la machine de reference (i3-2100, 8 Go, sans
encodage materiel). Si tu changes le pipeline, relance `python ultrak.py bench` et
mets a jour `CALIBRATION` dans `src/ultrak/planner.py` ainsi que le tableau du README.

## Couper une release

```bash
python tools/make_release.py                 # -> ultrak-source.zip (~53 Ko, sans ffmpeg.exe)
git tag -a v1.0.1 -m "note de version" && git push origin v1.0.1
gh release create v1.0.1 --verify-tag -t "UltraK v1.0.1" -F notes.md ultrak-source.zip
```

Le zip n'est pas un `git archive` : `make_release.py` vire les binaires et les videos,
qui font depasser la limite GitHub de 100 Mo par fichier.

La CI ne publie rien : les actions de ce depot tournent avec `GITHUB_TOKEN` en lecture
seule (Settings → Actions → General → Workflow permissions = « Read repository »), donc
un job d'API y recoit un `403 Resource not accessible by integration`. Pour automatiser,
passe ce reglage sur « Read and write » puis remets un job `softprops/action-gh-release`
avec `permissions: contents: write`.

## Avant d'ouvrir une PR

- `python -m pytest -q` et le lint des filtres passent sur ton OS.
- Les decisions du planner restent expliquees a l'utilisateur : aucune valeur ne doit
  etre choisie en silence.
- Les binaires (ffmpeg.exe) ne vont jamais dans git : 101 Mo, la limite GitHub est 100 Mo.
