import requests
import random, string
from concurrent import futures
from tqdm import tqdm
import time
from datetime import datetime
import argparse
import os
import sys
import shutil

def display_error(response, message):
	print(message)
	print(response)
	print(response.text)
	exit()


# Request from site all information about book, including titles, all metadata and such

def get_book_infos(session, url):
	r = session.get(url).text
	infos_url = "https:" + r.split('bookManifestUrl="')[1].split('"\n')[0]
	response = session.get(infos_url)
	data = response.json()['data']
	title = string.capwords(data['brOptions']['bookTitle']) # removed spaces replacing .replace(" ", "_")
	title = ''.join( c for c in title if c not in '<>:"/\\|?*' ) # Filter forbidden chars in directory names (Windows & Linux)
	title =  title[:150] + " " + url.split('/')[4] # Trim the title to avoid long file names and add book URL as modificator	
	metadata = data['metadata']
	links = []
	for item in data['brOptions']['data']:
		for page in item:
			links.append(page['uri'])

	if len(links) > 1:
		print(f"[+] Found {len(links)} pages")
		return title, links, metadata
	else:
		print(f"[-] Error while getting image links")
		exit()  # must raise exeption, not exit!

def format_data(content_type, fields):
	data = ""
	for name, value in fields.items():
		data += f"--{content_type}\x0d\x0aContent-Disposition: form-data; name=\"{name}\"\x0d\x0a\x0d\x0a{value}\x0d\x0a"
	data += content_type+"--"
	return data

def login(email, password):
	session = requests.Session()
	session.get("https://archive.org/account/login")
	content_type = "----WebKitFormBoundary"+"".join(random.sample(string.ascii_letters + string.digits, 16))

	headers = {'Content-Type': 'multipart/form-data; boundary='+content_type}
	data = format_data(content_type, {"username":email, "password":password, "submit_by_js":"true"})

	response = session.post("https://archive.org/account/login", data=data, headers=headers)
	if "bad_login" in response.text:
		print("[-] Invalid credentials!")
		exit()
	elif "Successful login" in response.text:
		print("[+] Successful login")
		return session
	else:
		display_error(response, "[-] Error while login:")

def loan(session, book_id, verbose=True):
	data = {
		"action": "grant_access",
		"identifier": book_id
	}
	# 2022-07-03: This request is done by the website but we don't need to do it here.
	# response = session.post("https://archive.org/services/loans/loan/searchInside.php", data=data)
	data['action'] = "browse_book"
	response = session.post("https://archive.org/services/loans/loan/", data=data)

	if response.status_code == 400 :
		if response.json()["error"] == "This book is not available to borrow at this time. Please try again later.":
			print("This book doesn't need to be borrowed")
			return session
		else :
			display_error(response, "Something went wrong when trying to borrow the book.")

	data['action'] = "create_token"
	response = session.post("https://archive.org/services/loans/loan/", data=data)

	if "token" in response.text:
		if verbose:
			print("[+] Successful loan")
		return session
	else:
		display_error(response, "Something went wrong when trying to borrow the book, maybe you can't borrow this book.")

# routine to return loan on selected book id

def return_loan(session, book_id):
	data = {
		"action": "return_loan",
		"identifier": book_id
	}
	response = session.post("https://archive.org/services/loans/loan/", data=data)
	if response.status_code == 200 and response.json()["success"]:
		print("[+] Book returned")
	else:
		display_error(response, "Something went wrong when trying to return the book") # else if we download multiple books we must not exit!

def image_name(pages, page, directory, book_id):
	return f"{directory}/{book_id}_{(len(str(pages)) - len(str(page))) * '0'}{page}.jpg"

def download_one_image(session, link, i, directory, book_id, pages):
	image = image_name(pages, i, directory, book_id)
	if not os.path.exists(image):
		headers = {
			"Referer": "https://archive.org/",
			"Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
			"Sec-Fetch-Site": "same-site",
			"Sec-Fetch-Mode": "no-cors",
			"Sec-Fetch-Dest": "image",
		}
		retry = True
		while retry:
			try:
				response = session.get(link, headers=headers)
				if response.status_code == 403:
					session = loan(session, book_id, verbose=False)
					raise Exception("Borrow again")
				elif response.status_code == 200:
					retry = False
			except KeyboardInterrupt:
				raise
			except:
				time.sleep(1)	# Wait 1 second before retrying

		tmpimage = image.replace(".jpg",".tmp")
		with open(tmpimage,"wb") as f:
			f.write(response.content)
		os.rename(tmpimage, image)

def download(session, n_threads, directory, links, scale, book_id):	
	print("Downloading pages...")
	links = [f"{link}&rotate=0&scale={scale}" for link in links]
	pages = len(links)

	tasks = []
	with futures.ThreadPoolExecutor(max_workers=n_threads) as executor:
		for link in links:
			i = links.index(link)
			tasks.append(executor.submit(download_one_image, session=session, link=link, i=i, directory=directory, book_id=book_id, pages=pages))
		for task in tqdm(futures.as_completed(tasks), total=len(tasks)):
			pass

	images = [image_name(pages, i, directory, book_id) for i in range(len(links))]
	return images

def make_pdf(pdf, title, directory):
	file = title+".pdf"
	# Write only if file does not exist
	if not os.path.isfile(os.path.join(directory, file)):
		with open(os.path.join(directory, file),"wb") as f:
			f.write(pdf)
		print(f"[+] PDF saved as \"{file}\"")
	else:
		print(f"[-] PDF file \"{file}\" already present on disk")

if __name__ == "__main__":

	print("Archive Downloader 2022.10.1")

	if len(sys.argv) == 1:
		print("Note that you can specify configuraion file in parameters like AD C:\Path\To\MyConfig.txt")

	# set default values

	email = "none"
	password = "none"
	scale = 0
	n_threads = 10
	d = os.getcwd()	
	outtype = "jpg"
	urls = []
	myfile = 'ad.txt'

	# use custom configuation file if supplied

	if len(sys.argv) == 2:
		myfile = sys.argv[1]

	if not os.path.isfile(myfile):
		print("Can't find configuration file, exiting!")
		exit(1)

	file1 = open(myfile, 'r')     

        # Set current parsing mode to none

	mode_pars = "none"
  
	while True:  
    		# Get next line from file
		line = file1.readline()		
		# if line is empty
		# end of file is reached
		if not line:
			break
		line = line.strip('\n ')    # remove all accidental spaces and newline
		if line.find("#") == 0:
			if line.find("# Your archive.org email")==0:
				mode_pars = "email"
			if line.find("# Your archive.org password")==0:
				mode_pars = "password"
			if line.find("# Output directory")==0:
				mode_pars = "outdir"
			if line.find("# Image resolution")==0:
				mode_pars = "resolution"
			if line.find("# Maximum number of threads")==0:
				mode_pars = "threads"
			if line.find("# Type of output - jpg")==0:
				mode_pars = "outtype"
			if line.find("# Folder and file naming")==0:
				mode_pars = "naming"

			if line.find("# Book URLs")==0:
				mode_pars = "urls"
			continue
		if  mode_pars ==  "email":
			email = line		
		if  mode_pars ==  "password":
			password = line	
		if  mode_pars ==  "outdir":
			d = line	
		if  mode_pars ==  "resolution":
			scale = int(line)
		if  mode_pars ==  "threads":
			n_threads = int(line)
		if  mode_pars ==  "outtype":
			outtype = line

		if mode_pars == "urls":
			urls.append(line.strip())
		else:
			mode_pars = "none"
  
	file1.close()

	if not os.path.isdir(d):
		print(f"Output directory does not exist!")
		exit()

	books = []

	# Check the urls format
	for url in urls:
		if url.startswith("https://archive.org/details/"):
			book_id = list(filter(None, url.split("/")))[3]
			books.append((book_id, url))
		elif len(url.split("/")) == 1:
			books.append((url, "https://archive.org/details/" + url))
		else:
			print(f"{url} --> Invalid book. URL must start with \"https://archive.org/details/\", or be a book id without any \"/\"")
			exit()
		
	print(f"{len(books)} Book(s) to download")

	session = login(email, password)

	for book in books:
		book_id = book[0]
		url = book[1]
		print("="*40)
		print(f"Current book: https://archive.org/details/{book_id}")
		session = loan(session, book_id)
		title, links, metadata = get_book_infos(session, url)

		directory = os.path.join(d, title)

		if not os.path.isdir(directory):
			os.makedirs(directory)

		images = download(session, n_threads, directory, links, scale, book_id)

		if outtype in ("pdf","jpgpdf","jpgpdfmeta"): # any modes that require creation of PDF file
			import img2pdf

			# prepare PDF metadata
                        # Use  string.capwords() on title and more
                        # keywords are in 'subject'  
                        # ISBN can be got from 'scribe3_search_catalog': 'isbn', 'scribe3_search_id': '9780981803982', 
                        # or isbn': ['9780981803982', '0981803989']
                        # 'creator': 'Kingsley, Eve', 'date': '2008'
			# sometimes archive metadata is missing
			pdfmeta = { }
			# ensure metadata are str
			for key in ["title", "creator", "associated-names"]:
				if key in metadata:
					if isinstance(metadata[key], str):
						pass
					elif isinstance(metadata[key], list):
						metadata[key] = "; ".join(metadata[key])
					else:
						raise Exception("unsupported metadata type")
			# title
			if 'title' in metadata:
				pdfmeta['title'] = string.capwords(metadata['title'])

			# author, we have issue here as we need sometimes to modify names from Rayan, Jack to Jack Rayan

			authors_list = ""

			if 'creator' in metadata:
				authors_list = metadata['creator']
			if 'associated-names' in metadata:
				if not authors_list == "":
					authors_list = authors_list  + ";"
				authors_list = authors_list + metadata['associated-names']

			authors_split = authors_list.split(";")
			authors_list = ""

			for author in authors_split:
				author_res=""
				for ch in author:
					if ch not in ['0','1','2','3','4','5','6','7','8','9','-']:
						author_res+=ch
				if not author_res.find(",")==-1:
					author_split = author_res.split(",")
					author_res = author_split[1].strip()+" "+author_split[0].strip()
				if authors_list=="":
					authors_list = author_res
				else:
					authors_list = authors_list + " & " + author_res

			pdfmeta['author'] = authors_list

			# print(metadata)

			if 'date' in metadata:
				try:
					pdfmeta['creationdate'] = datetime.strptime("1 June " + metadata['date'], '%d %B %Y')
					pdfmeta['moddate'] = datetime.strptime("1 June " + metadata['date'], '%d %B %Y')
				except:
					pass
			# keywords

			pdfmeta['keywords'] = metadata['subject']+[f"https://archive.org/details/{book_id}"]+metadata['isbn']+metadata['date']

			# print(metadata['subject'])

			if outtype=="jpgpdfmeta":
				images = images[0] 

			pdf = img2pdf.convert(images, **pdfmeta)

			make_pdf(pdf, title, d)
			if outtype=="pdf":
				try:
					shutil.rmtree(directory)
				except OSError as e:
					print ("Error: %s - %s." % (e.filename, e.strerror))

		return_loan(session, book_id)