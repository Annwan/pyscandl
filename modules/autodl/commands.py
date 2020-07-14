import os, sys
import json
import requests
import cfscrape
import re
from xml.etree import ElementTree
from ..excepts import IsStandalone, FetcherNotFound, EmptyChapter
from ..Pyscandl import Pyscandl
from ..fetchers.fetcher_enum import Fetchers


class Controller:
	__doc__ = """
	Object responsible of the autodl part of the program, all the logic related to it passes here
	"""

	def __init__(self, output:str=".", quiet:bool=False, tiny:bool=False):
		"""
		Initializes this instance of the autodl controller.
		If there is no json database for autodl currently in existence a new one is created at ``pyscandl/modules/autodl/db.json``.

		:param output: location where the outputted scans should be stored
		:type output: str
		:param quiet: should the program not output any information about what it is doing in the console
		:type quiet: bool
		:param tiny: should the name of every downloaded scan be minified and only include the chapter number and the chapter title
		:type tiny: bool
		"""

		try:
			with open(f"{os.path.dirname(sys.modules['modules.autodl'].__file__)}/db.json", "r") as data:
				self.db = json.load(data)
		except FileNotFoundError:
			self.db = {}
		self.output = output
		self.quiet = quiet
		self.tiny = tiny
		self._re_mgdex_scan = re.compile(r"(?:Chapter \d+, )?Chapter (?P<chap>\d+(\.\d+)?)")
		self.scrapper = cfscrape.create_scraper()
		self.missing_chaps = []
		self.downloads = 0

	def save(self):
		"""
		Saves the current state of the database in the ``db.json`` file.
		"""

		with open(f"{os.path.dirname(sys.modules['modules.autodl'].__file__)}/db.json", "w") as data:
			json.dump(self.db, data, indent=4, sort_keys=True)

	def add(self, name:str, rss:str, link:str, fetcher:str, chapters:list=[]):
		"""
		Adds a new scan entry to the ``db.json`` file.

		:param name: name of the manga
		:type name: str
		:param rss: rss link of the manga
		:type rss: str
		:param link: link to the page of the manga *(same link that is used for the -l arg in other uses of pyscandl)*
		:type link: str
		:param fetcher: name of the associated fetcher
		:type fetcher: str
		:param chapters: list of the already possessed chapters that wont be downloaded again *(Optional)*
		:type chapters: list[int/float/str]

		:raises FetcherNotFound: the specified fetcher doesn't exist
		:raises IsStandalone: the specified fetcher is a standalone fetcher
		"""

		if fetcher.upper() not in [i.name for i in Fetchers]:
			raise FetcherNotFound(fetcher)
		if fetcher.lower() in ["nhentai"]:
			raise IsStandalone(name)
		self.db[name] = {
			"rss": rss,
			"link": link,
			"fetcher": fetcher.upper(),
			"chapters": sorted(chapters, reverse=True)
		}

	def edit(self, name:str, rss:str=None, link:str=None, fetcher=None, chapters:list=None):
		"""
		Edits an already existing entry in the ``db.json`` file.
		The :param name: is mandatory to find the correct entry and every other parameter specified will overwrite the existing values.

		:param name: name of the manga
		:type name: str
		:param rss: rss link of the manga
		:type rss: str
		:param link: link to the page of the manga *(same link that is used for the -l arg in other uses of pyscandl)*
		:type link: str
		:param fetcher: name of the associated fetcher
		:type fetcher: str
		:param chapters: list of the already possessed chapters that wont be downloaded again
		:type chapters: list[int/float/str]

		:raises IsStandalone: the specified fetcher is a standalone fetcher
		"""

		if rss is not None:
			self.db.get(name)["rss"] = rss
		if link is not None:
			self.db.get(name)["link"] = link
		if fetcher is not None:
			standalone_check = Fetchers.get(fetcher)
			if standalone_check.standalone:
				raise IsStandalone(name)
			self.db.get(name)["fetcher"] = fetcher
		if chapters is not None:
			self.db.get(name)["chapters"] = sorted(self.db.get(name)["chapters"] + chapters, reverse=True)

	# each website/fetcher can have differently made xml from their rss so we need to treat them separately if need be
	def scan(self, name:str):
		"""
		Scans the asked manga for new and non downloaded chapters and adds them to the controller queue.

		:param name: name of the manga
		:type name: str
		"""

		self.missing_chaps.clear()
		manga = self.db.get(name)
		if manga.get("fetcher").lower() in ["fanfox", "fanfox_mono"]:
			xml = ElementTree.fromstring(requests.get(manga.get("rss")).content)
			for chapter in xml.iter("item"):
				nb = chapter.find("link").text.split("/")[-2][1:]
				if "." in nb:
					nb = float(nb)
				else:
					nb = int(nb)
				if nb not in manga.get("chapters"):
					self.missing_chaps.append(nb)
		elif manga.get("fetcher").lower() == "mangadex":
			raw = self.scrapper.get(manga.get("rss")).text
			if "The Database server is under heavy load and can't serve your request. Please wait a bit and try refreshing the page." in raw:
				if not self.quiet:
					print(f"The Database server of mangadex is under heavy load and can't check the chapters of \"{name}\" at the moment.")
			else:
				xml = ElementTree.fromstring(raw)
				for chapter in xml.iter("item"):
					if chapter.find("description").text.split(" - ")[-1] == "Language: English":
						# check if it's a chapter
						if self._re_mgdex_scan.search(chapter.find("title").text):
							nb = self._re_mgdex_scan.search(chapter.find("title").text).group("chap")
							if "." in nb:
								nb = float(nb)
							else:
								nb = int(nb)
							if nb not in manga.get("chapters"):
								self.missing_chaps.append(nb)
		self.missing_chaps.sort()
		if not self.quiet:
			if self.missing_chaps:
				print(f"new chapter(s) for {name}: {', '.join(map(str, self.missing_chaps))}")
			else:
				print(f"no new chapter for {name}")

	def download(self, name:str, pdf:bool=True, keep:bool=False, image:bool=False):
		"""
		Start the download of the chapters of the asked manga that have their number in the scan results.

		:param name: name of the manga
		:type name: str
		:param pdf: tell if the result should be kept as a pdf
		:type pdf: bool
		:param keep: tell if the result should be kept as a pdf and as a collection of images
		:type keep: bool
		:param image: tell if the result should be kept as a collection of images
		:type image: bool
		"""

		manga = self.db.get(name)
		fetcher = Fetchers.get(manga.get("fetcher"))

		# initialize to the first downloadable chapter and download it
		ok = False
		for chapter_id in range(len(self.missing_chaps)):
			try:
				downloader = Pyscandl(fetcher, self.missing_chaps[chapter_id], self.output, link=manga.get("link"), quiet=self.quiet, tiny=self.tiny)

				bad_image = True
				while bad_image:  # protect against bad downloads
					try:
						if keep or image:
							downloader.keep_full_chapter()
						elif pdf:
							downloader.full_chapter()
						if not image:
							downloader.create_pdf()
						bad_image = False
					except IOError:
						print(f"problem during download, retrying {name} chapter {self.missing_chaps[chapter_id]}")
						downloader.go_to_chapter(self.missing_chaps[chapter_id])

				self.db.get(name).get("chapters").append(self.missing_chaps[chapter_id])
				self.downloads += 1

				self.missing_chaps = self.missing_chaps[chapter_id+1:]
				ok = True
				break
			except EmptyChapter:
				if not self.quiet:
					print(f"skipping {name} chapter {self.missing_chaps[chapter_id]}: empty, wont be added in the downloaded list")

		# if chapters are left to doawnload proceeds with it
		if ok:
			for chapter_id in range(len(self.missing_chaps)):
				try:
					bad_image = True
					while bad_image:  # protect against bad downloads
						try:
							downloader.go_to_chapter(self.missing_chaps[chapter_id])

							if keep or image:
								downloader.keep_full_chapter()
							else:
								downloader.full_chapter()
							if not image:
								downloader.create_pdf()
							bad_image = False
						except IOError:
							print(f"problem during download, retrying {name} chapter {self.missing_chaps[chapter_id]}")

					self.db.get(name).get("chapters").append(self.missing_chaps[chapter_id])
					self.downloads += 1
				except EmptyChapter:
					if not self.quiet:
						print(f"skipping {name} chapter {self.missing_chaps[chapter_id]}: empty, wont be added in the downloaded list")

			downloader.fetcher.quit()
			self.db.get(name).get("chapters").sort(reverse=True)

		# remove the directory if there is no chapter
		try:
			folders = list(os.walk(self.output))[1:]
			for folder in folders:
				if not folder[2]:
					os.rmdir(folder[0])
		except OSError:
			pass

	def list_mangas(self):
		"""
		Gives the list of all the names of the mangas in the ``db.json`` file.

		:rtype: dict_keys
		"""

		return list(self.db.keys())

	def manga_info(self, name):
		"""
		Fet the infos about a specific manga.

		:param name: name of the manga
		:type name: str

		:rtype: dict
		"""

		return self.db.get(name)

	def delete_manga(self, name):
		"""
		Deletes a manga from the ``db.json`` file.

		:param name: name of the manga

		:return: confirms the deletion
		:rtype: bool
		"""
		if name in self.db:
			del self.db[name]
			return True
		else:
			return False

	def rm_chaps(self, name, *rm_chaps):
		"""
		Remove the listed chapters from the asked manga

		:param name: name of he manga
		:type name: str
		:param rm_chaps: list of all the chapters that have to be removed
		:type rm_chaps: str

		:return: confirms the deletion
		:rtype: bool
		"""
		if name in self.db:
			self.db.get(name)["chapters"] = [chap for chap in self.db.get(name)["chapters"] if not chap not in rm_chaps]
			return True
		else:
			return False
