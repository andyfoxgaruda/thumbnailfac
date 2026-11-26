import html, json, secrets, hmac, re
from pathlib import Path
from tempfile import TemporaryFile
from urllib.parse import quote
from zipfile import ZipFile
from fastapi import FastAPI, UploadFile, File, Form, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, StreamingResponse
from review_jobs import ReviewJobs, ReviewError
from art_dna import ArtDNA, ArtDNAError
from reference_analysis import build_reference_index

ROOT=Path(__file__).resolve().parent
INCOMING=ROOT/"Incoming"; EXPORTS=ROOT/"Exports"; PROJECTS=ROOT/"Projects"
REFERENCE=ROOT/"ReferenceLibrary"
ANALYSIS_INDEX=ROOT/"ReferenceAnalysis"/"image_index.json"
ASSETS=ROOT/"Assets"/"Downloaded"
for folder in (INCOMING,EXPORTS,PROJECTS,REFERENCE,ASSETS):
    folder.mkdir(parents=True,exist_ok=True)

S=json.loads((ROOT/"secrets.json").read_text(encoding="utf-8"))
PASSWORD=S["portal_password"]
REVIEW_JOBS=ReviewJobs(ROOT/"Jobs"/"PendingReview")
ART_DNA=ArtDNA(ROOT/"ArtDNA"/"art_dna.db",REFERENCE)
app=FastAPI(title="Thumbnail Upload Portal")
MAX_PER_FILE=30*1024*1024
ALLOWED={".png",".jpg",".jpeg",".webp",".bmp",".tif",".tiff",".psd",".psb"}

def logged_in(request: Request):
    value=request.cookies.get("thumb_auth","")
    return hmac.compare_digest(value,PASSWORD)


def approval_token(job_id):
    return hmac.digest(PASSWORD.encode(), ("studio-approve:"+job_id).encode(), "sha256").hex()


@app.get("/review")
def review_redirect():
    return RedirectResponse("/?tab=review",status_code=303)


def review_content():
    sections = []
    for manifest in REVIEW_JOBS.jobs():
        job = manifest["job_id"]
        rows = []
        for number,mode in enumerate(manifest["modes"],1):
            label = f"Image {number} · {mode.replace('_',' ').title()}"
            approved = REVIEW_JOBS.approved(EXPORTS,job,number)
            candidate = REVIEW_JOBS.pending(job,number)
            try:
                feedback = ART_DNA.feedback_for(job,number)
            except ArtDNAError:
                feedback = None
            if approved.is_file():
                rows.append(f"<p>{label} · Approved · <a href='/download/{approved.name}'>Download approved PNG</a></p>")
            elif feedback and feedback["decision"] == "reject" and candidate.is_file():
                rows.append(f"<p>{label} · Passed · feedback saved</p><img alt='{label}' loading='lazy' src='/review/{job}/{number}'>")
            elif candidate.is_file():
                rows.append(f"<p><label><input type='checkbox' name='numbers' value='{number}'> {label}</label></p>"
                            f"<img alt='{label}' loading='lazy' src='/review/{job}/{number}'> "
                            f"<button type='submit' name='reject_number' value='{number}' "
                            f"formaction='/review/{job}/reject' formmethod='post'>Pass on this image</button>")
            else:
                rows.append(f"<p>{label} · Editing</p>")
        forms = "".join(rows)
        button = "<p><button>Approve selected images</button></p>" if "checkbox" in forms else ""
        note = "<label>What should Art DNA remember? (optional) " \
               "<input name='feedback_note' maxlength='240' placeholder='e.g. The title is easy to read'></label>"
        sections.append(f"<section><h2>{html.escape(manifest['title'])}</h2>"
                        f"<form method='post' action='/review/{job}/approve'>"
                        f"<input type='hidden' name='csrf' value='{approval_token(job)}'>{forms}{note}{button}</form></section>")
    return "".join(sections) or "<p>No review jobs yet.</p>"


@app.get("/review/{job_id}/{number}")
def review_image(job_id: str, number: int, request: Request):
    if not logged_in(request):
        raise HTTPException(401)
    try:
        path = REVIEW_JOBS.pending(job_id,number)
    except ReviewError as exc:
        raise HTTPException(404) from exc
    if not path.is_file():
        raise HTTPException(404)
    return FileResponse(path,media_type="image/png",headers={"Cache-Control":"private, no-store",
                        "X-Content-Type-Options":"nosniff"})


@app.post("/review/{job_id}/approve")
def approve_review(job_id: str, request: Request, csrf: str=Form(...), numbers: list[int]=Form(...),
                   feedback_note: str=Form("")):
    if not logged_in(request):
        raise HTTPException(401)
    if not hmac.compare_digest(csrf.encode(),approval_token(job_id).encode()):
        raise HTTPException(403)
    try:
        REVIEW_JOBS.approve(EXPORTS,job_id,numbers)
        manifest=REVIEW_JOBS.read(job_id)
        for number in dict.fromkeys(numbers):
            ART_DNA.record_feedback(job_id,number,"approve",manifest["modes"][number-1],
                                    manifest["title"],feedback_note,
                                    REVIEW_JOBS.pending(job_id,number))
    except ReviewError as exc:
        raise HTTPException(400,str(exc)) from exc
    except ArtDNAError as exc:
        raise HTTPException(400,str(exc)) from exc
    return RedirectResponse("/?tab=review",status_code=303)


@app.post("/review/{job_id}/reject")
def reject_review(job_id: str, request: Request, csrf: str=Form(...), reject_number: int=Form(...),
                  feedback_note: str=Form("")):
    if not logged_in(request):
        raise HTTPException(401)
    if not hmac.compare_digest(csrf.encode(),approval_token(job_id).encode()):
        raise HTTPException(403)
    try:
        manifest=REVIEW_JOBS.read(job_id)
        if REVIEW_JOBS.approved(EXPORTS,job_id,reject_number).exists():
            raise ReviewError("An approved image cannot be marked as passed.")
        ART_DNA.record_feedback(job_id,reject_number,"reject",manifest["modes"][reject_number-1],
                                manifest["title"],feedback_note,REVIEW_JOBS.pending(job_id,reject_number))
    except (ReviewError, ArtDNAError, IndexError) as exc:
        raise HTTPException(400,str(exc) or "The selected image is unavailable.") from exc
    return RedirectResponse("/?tab=review",status_code=303)

def safe_upload_name(filename):
    name=Path(filename or "upload").name
    ext=Path(name).suffix.lower()
    if ext not in ALLOWED: raise HTTPException(400,f"Unsupported file type: {ext}")
    stem=re.sub(r"[^A-Za-z0-9_. -]","_",Path(name).stem)[:70] or "image"
    return stem,ext

def unique_destination(folder,stem,ext):
    dest=folder/(stem+ext); i=2
    while dest.exists():
        dest=folder/(stem+f"_{i}"+ext); i+=1
    return dest

@app.get("/",response_class=HTMLResponse)
async def home(request: Request, tab: str="finished"):
    if not logged_in(request):
        return HTMLResponse("""<!doctype html><meta name=viewport content='width=device-width,initial-scale=1'><title>Thumbnail Workshop</title><style>body{font:16px Arial;max-width:650px;margin:8vh auto;padding:20px;background:#111;color:#eee}input,button{padding:12px;margin:8px 0;width:100%;box-sizing:border-box}button{background:#3478f6;color:white;border:0;border-radius:6px}</style><h2>Thumbnail Workshop</h2><p>Enter the upload password provided by the owner.</p><form method=post action=/login><input name=password type=password placeholder='Upload password' required><button>Unlock</button></form>""")
    panels = {"finished", "review", "upload"}
    if tab not in panels:
        tab = "finished"
    if tab == "review":
        content = review_content()
    elif tab == "upload":
        try:
            status=ART_DNA.status()
            analysis_status=(f"<p>Analysis library: {status['files']} images indexed; "
                             f"{status['annotated_images']} have visual notes. New Analysis uploads index automatically.</p>")
        except ArtDNAError:
            analysis_status="<p>Analysis uploads are indexed automatically and become available for visual notes.</p>"
        content = """<form class="upload" method="post" action="/upload" enctype="multipart/form-data">
        <label for="destination">Upload to</label>
        <select id="destination" name="destination">
        <option value="Assets">Assets · use in PhotoCraft</option>
        <option value="ReferenceLibrary">Analysis · Reference Library</option>
        </select>
        <label for="files">Select images or projects</label>
        <input id="files" type="file" name="files" accept="image/*,.psd,.psb" multiple required>
        <button>Upload</button></form>"""
        content += analysis_status
    else:
        tiles = []
        for path in sorted(EXPORTS.iterdir(), key=lambda p:p.stat().st_mtime_ns, reverse=True):
            if not path.is_file() or path.suffix.lower() != ".png":
                continue
            url = "/download/" + quote(path.name,safe="")
            label = html.escape(path.name,quote=True)
            tiles.append(f"""<div class="tile-wrap"><button type="button" class="tile" data-url="{url}" data-name="{label}"
                aria-label="Open thumbnail"><img src="{url}?preview=1" alt="" loading="lazy" decoding="async"></button>
                <label class="pick"><input type="checkbox" name="names" value="{label}" aria-label="Select thumbnail {len(tiles)+1}"></label></div>""")
        content = ('<form id="takeout" method="post" action="/takeout">'
                   '<p><button type="submit" disabled>Download selected (.zip)</button> '
                   '<button type="button" id="download-images" disabled>Download images</button> '
                   '<output aria-live="polite">0 selected</output></p>'
                   '<div class="tiles">'+"".join(tiles)+"</div></form>") if tiles else "<p>No thumbnails yet.</p>"
    tabs = "".join(
        f'<a href="/?tab={key}"'+(' aria-current="page"' if key==tab else '')+f'>{label}</a>'
        for key,label in (("finished","Takeout"),("review","Ongoing"),("upload","Upload"))
    )
    return HTMLResponse("""<!doctype html><html lang="en"><meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1"><title>Thumbnail Workshop</title>
    <style>
    :root{color-scheme:dark}*{box-sizing:border-box}body{margin:0;background:#101116;color:#edf0f9;font:16px system-ui}
    header,main{max-width:1240px;margin:auto;padding:20px clamp(14px,4vw,40px)}
    .bar{display:flex;justify-content:space-between;align-items:center;gap:12px}h1{font-size:clamp(20px,4vw,30px);margin:0}
    nav{display:flex;gap:6px;overflow:auto;margin-top:22px;border-bottom:1px solid #36394a}
    nav a{padding:12px 10px;white-space:nowrap;text-decoration:none;color:#b9c2d5;border-bottom:2px solid transparent}
    nav a[aria-current]{color:#fff;border-color:#9c9aff;font-weight:650}a{color:#b6bdff}
    button{cursor:pointer;border:1px solid #4a4d61;border-radius:8px;background:#262938;color:inherit;padding:9px 12px;font:inherit}
    :focus-visible{outline:2px solid #b5b1ff;outline-offset:3px}
    .tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,170px),1fr));gap:13px}
    .tile-wrap{position:relative}.tile{width:100%;padding:0;background:#1a1c27;overflow:hidden;aspect-ratio:4/3}
    .pick{position:absolute;top:8px;right:8px;background:#101116dd;border-radius:6px;padding:5px;cursor:pointer}
    .pick input{width:20px;height:20px;margin:0;vertical-align:middle;accent-color:#9c9aff}
    .tile-wrap:has(input:checked) .tile{outline:2px solid #9c9aff;outline-offset:-2px}
    button:disabled{opacity:.45;cursor:default}
    .tile img{display:block;width:100%;height:100%;object-fit:contain}
    section,.upload{padding:16px;background:#1c1e2a;border:1px solid #35384a;border-radius:12px;margin-bottom:14px}
    section img{display:block;width:min(100%,470px);height:auto}.upload{display:grid;gap:15px;max-width:520px}
    dialog{width:min(92vw,920px);padding:16px;border:1px solid #4a4d61;border-radius:12px;background:#1a1c27;color:inherit}
    dialog::backdrop{background:#080910cc}dialog img{display:block;width:100%;max-height:72vh;object-fit:contain}
    dialog strong{overflow-wrap:anywhere}dialog footer{margin-top:12px}
    </style><header><div class="bar"><h1>Thumbnail Workshop</h1>
    <form method="post" action="/logout"><button>Lock</button></form></div>
    <nav aria-label="Workshop tabs">"""+tabs+"""</nav></header><main>"""+content+"""</main>
    <dialog aria-labelledby="image-name"><div class="bar"><strong id="image-name"></strong>
    <form method="dialog"><button>Close</button></form></div><img alt="Selected thumbnail"><footer><a download>Download PNG</a></footer></dialog>
    <script>
    const viewer=document.querySelector('dialog');
    const takeout=document.querySelector('#takeout');
    if(takeout){
      const direct=takeout.querySelector('#download-images');
      const zip=takeout.querySelector('button[type=submit]');
      const status=takeout.querySelector('output');
      let downloading=false;
      const update=()=>{
        const count=takeout.querySelectorAll('input:checked').length;
        direct.disabled=zip.disabled=downloading||count===0;
        if(!downloading) status.textContent=count+' selected';
      };
      takeout.addEventListener('change',update);
      direct.addEventListener('click',async()=>{
        if(downloading) return;
        const selected=[...takeout.querySelectorAll('input:checked')];
        if(!selected.length) return;
        downloading=true;
        update();
        try {
          for(const [index,box] of selected.entries()){
            const tile=box.closest('.tile-wrap').querySelector('.tile');
            const link=document.createElement('a');
            link.href=tile.dataset.url;
            link.download=tile.dataset.name;
            document.body.append(link);
            link.click();
            link.remove();
            status.textContent=(index+1)+' of '+selected.length+' downloads requested';
            if(index+1<selected.length) await new Promise(resolve=>setTimeout(resolve,450));
          }
        } finally {
          downloading=false;
          update();
          status.textContent=selected.length+' downloads requested. Allow multiple downloads if prompted.';
        }
      });
      update();
    }
    for(const tile of document.querySelectorAll('.tile')) tile.addEventListener('click',()=>{
      viewer.querySelector('strong').textContent=tile.dataset.name;
      viewer.querySelector('img').src=tile.dataset.url+'?preview=1';
      viewer.querySelector('a').href=tile.dataset.url;
      viewer.showModal();
    });
    </script></html>""",headers={"Cache-Control":"private, no-store"})

@app.post("/login")
async def login(password: str=Form(...)):
    if not hmac.compare_digest(password,PASSWORD): raise HTTPException(401,"Wrong password")
    r=RedirectResponse("/",status_code=303)
    r.set_cookie("thumb_auth",PASSWORD,httponly=True,secure=True,samesite="strict",max_age=86400)
    return r

@app.post("/logout")
async def logout():
    r=RedirectResponse("/",status_code=303); r.delete_cookie("thumb_auth"); return r

@app.post("/upload")
async def upload(request: Request, files: list[UploadFile]=File(...), destination: str=Form("Incoming")):
    if not logged_in(request): raise HTTPException(401,"Please unlock the portal first")
    folders={"ReferenceLibrary":REFERENCE,"Assets":ASSETS,"Incoming":INCOMING}
    if destination not in folders: raise HTTPException(400,"Invalid upload destination")
    folder=folders[destination]
    saved=[]; errors=[]
    for file in files:
        stem,ext=safe_upload_name(file.filename)
        dest=unique_destination(folder,stem,ext)
        total=0
        try:
            with dest.open("wb") as out:
                while True:
                    chunk=await file.read(1024*1024)
                    if not chunk: break
                    total+=len(chunk)
                    if total>MAX_PER_FILE:
                        raise HTTPException(413,f"{file.filename} exceeds 30 MB")
                    out.write(chunk)
            if total==0:
                dest.unlink(missing_ok=True)
                errors.append(f"{file.filename}: empty file")
            else:
                saved.append(f"{dest.name} ({total//1024} KB)")
        except Exception:
            dest.unlink(missing_ok=True)
            raise
        finally:
            await file.close()
    message="Uploaded: "+"; ".join(saved) if saved else "No files uploaded."
    if destination=="ReferenceLibrary" and saved:
        try:
            indexed=ART_DNA.index()
            analysis_index=build_reference_index(REFERENCE,ANALYSIS_INDEX)
            message+=(f". Analysis refreshed: {indexed['new_content']} new Art DNA images; "
                      f"{analysis_index['count']} images available for visual notes; "
                      f"{len(indexed['errors'])} unreadable files.")
        except (ArtDNAError, OSError):
            message+=". Files were uploaded, but one of the analysis indexes could not be refreshed."
    if errors: message+=" Errors: "+"; ".join(errors)
    return HTMLResponse(f"<meta http-equiv='refresh' content='2;url=/'><body style='font:16px Arial;background:#111;color:#eee;padding:30px'><h2>{message}</h2><p>Returning to the portal…</p></body>")

@app.post("/takeout")
def takeout(request: Request, names: list[str]=Form(...)):
    if not logged_in(request):
        raise HTTPException(401)
    names = list(dict.fromkeys(names))
    if not 1<=len(names)<=100:
        raise HTTPException(400,"Select between 1 and 100 thumbnails.")
    root = EXPORTS.resolve()
    paths = []
    for name in names:
        path = (root/name).resolve()
        if Path(name).name != name or path.parent != root or path.suffix.lower() != ".png" or not path.is_file():
            raise HTTPException(404,"A selected thumbnail is unavailable.")
        paths.append(path)
    if sum(path.stat().st_size for path in paths)>512*1024*1024:
        raise HTTPException(413,"Select fewer thumbnails for this download.")
    archive = TemporaryFile()
    try:
        with ZipFile(archive,"w") as batch:
            for path in paths:
                batch.write(path,arcname=path.name)
        archive.seek(0)
    except BaseException:
        archive.close()
        raise

    def chunks():
        with archive:
            while chunk := archive.read(65536):
                yield chunk

    return StreamingResponse(chunks(),media_type="application/zip",headers={
        "Content-Disposition":'attachment; filename="thumbnail-takeout.zip"',
        "Cache-Control":"private, no-store",
    })


@app.get("/download/{name}")
async def download(name: str, request: Request, preview: bool=False):
    if not logged_in(request): raise HTTPException(401,"Please unlock the portal first")
    name=Path(name).name; p=(EXPORTS/name).resolve()
    if p.parent!=EXPORTS.resolve() or not p.is_file(): raise HTTPException(404,"File not found")
    if preview and p.suffix.lower()==".png":
        return FileResponse(p, media_type="image/png", headers={"Cache-Control":"private, no-store", "X-Content-Type-Options":"nosniff"})
    return FileResponse(p,filename=p.name)

@app.get("/health")
async def health(): return {"ok":True,"service":"Thumbnail upload portal"}

if __name__=="__main__":
    import uvicorn
    uvicorn.run(app,host="127.0.0.1",port=8001,log_level="info")
