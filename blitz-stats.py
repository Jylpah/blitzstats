#!/usr/bin/env python3

# Script fetch Blitz player stats and tank stats

import sys, argparse, json, os, inspect, pprint, aiohttp, asyncio, aiofiles, aioconsole
import motor.motor_asyncio, ssl, lxml, re, logging, time, xmltodict, collections, pymongo
import configparser
import blitzutils as bu
import blitzstatsutils as su
from bs4 import BeautifulSoup
from blitzutils import BlitzStars, WG, WoTinspector, RecordLogger

logging.getLogger("asyncio").setLevel(logging.DEBUG)

N_WORKERS = 20
MAX_RETRIES = 3
CACHE_VALID = 24*3600*5   # 5 days
SLEEP = 1
REPLAY_N = 0

WG_appID = 'cd770f38988839d7ab858d1cbe54bdd0'

FILE_ACTIVE_PLAYERS	= 'activeinlast30days.json'

db = None
wi = None
bs = None
WI_STOP_SPIDER = False
WI_old_replay_N = 0
WI_old_replay_limit = 25

## main() -------------------------------------------------------------

async def main(argv):
	# set the directory for the script
	os.chdir(os.path.dirname(sys.argv[0]))

	global db, wi, bs, MAX_PAGES

	# Default params
	MAX_PAGES 	= 500
	N_WORKERS 	= 20
	FILE_CONFIG = 'blitzstats.ini'
	DB_SERVER 	= 'localhost'
	DB_PORT 	= 27017
	DB_TLS		= False
	DB_CERT_REQ = False
	DB_AUTH 	= 'admin'
	DB_NAME 	= 'BlitzStats'
	DB_USER		= 'mongouser'
	DB_PASSWD 	= None
	DB_CERT 	= None
	DB_CA 		= None

	## Read config
	if os.path.isfile(FILE_CONFIG):
		config = configparser.ConfigParser()
		config.read(FILE_CONFIG)
		if 'OPTIONS' in config.sections():
			configOpts	= config['OPTIONS']
			MAX_PAGES   = configOpts.getint('opt_get_account_ids_max_pages', MAX_PAGES)
		if 'DATABASE' in config.sections():
			configDB 	= config['DATABASE']
			DB_SERVER 	= configDB.get('db_server', DB_SERVER)
			DB_PORT 	= configDB.getint('db_port', DB_PORT)
			DB_TLS		= configDB.getboolean('db_tls', DB_TLS)
			DB_CERT_REQ = configDB.getboolean('db_tls_req', DB_CERT_REQ)
			DB_AUTH 	= configDB.get('db_auth', DB_AUTH)
			DB_NAME 	= configDB.get('db_name', DB_NAME)
			DB_USER		= configDB.get('db_user', DB_USER)
			DB_PASSWD 	= configDB.get('db_password', DB_PASSWD)
			DB_CERT		= configDB.get('db_tls_cert_file', DB_CERT)
			DB_CA		= configDB.get('db_tls_ca_file', DB_CA)

	## DB command line args TBD

	# db_parser = argparse.ArgumentParser(description='Set DB parameters')
	# db_parser.add_argument('--db_tls', action='store_true', default=DB_TLS, help='Use TLS for DB')
	# db_args, argv = db_parser.parse_known_args()

	try:
		bu.debug('DB_SERVER: ' + DB_SERVER)
		bu.debug('DB_PORT: ' + str(DB_PORT))
		bu.debug('DB_TLS: ' + "True" if DB_TLS else "False")
		bu.debug('DB_AUTH: ' + DB_AUTH)
		bu.debug('DB_NAME: ' + DB_NAME)
		
		#### Connect to MongoDB
		if (DB_TLS==False):
			if (DB_USER==None) or (DB_PASSWD==None):
				client = motor.motor_asyncio.AsyncIOMotorClient(DB_SERVER,DB_PORT, tls=DB_TLS)
			else:
				client = motor.motor_asyncio.AsyncIOMotorClient(DB_SERVER,DB_PORT, authSource=DB_AUTH, username=DB_USER, password=DB_PASSWD, tls=DB_TLS)
		else:     
			if (DB_USER==None) or (DB_PASSWD==None):
				client = motor.motor_asyncio.AsyncIOMotorClient(DB_SERVER,DB_PORT, tls=DB_TLS, tlsAllowInvalidCertificates=DB_CERT_REQ, tlsCertificateKeyFile=DB_CERT, tlsCAFile=DB_CA)
			else:
				client = motor.motor_asyncio.AsyncIOMotorClient(DB_SERVER,DB_PORT, authSource=DB_AUTH, username=DB_USER, password=DB_PASSWD, tls=DB_TLS, tlsAllowInvalidCertificates=DB_CERT_REQ, tlsCertificateKeyFile=DB_CERT, tlsCAFile=DB_CA)
		db = client[DB_NAME]
	except Exception as err:
		bu.error('Error connecting DB: ' + str(type(err)) + ' : '+ str(err)) 
		bu.verbose_std('DB_SERVER: ' + DB_SERVER)
		bu.verbose_std('DB_PORT: ' + str(DB_PORT))
		bu.verbose_std('DB_TLS: ' + "True" if DB_TLS else "False")
		bu.verbose_std('DB_AUTH: ' + DB_AUTH)
		bu.verbose_std('DB_NAME: ' + DB_NAME)	

	parser = argparse.ArgumentParser(description='Fetch and manage WoT Blitz stats')
	arggroup = parser.add_mutually_exclusive_group()
	arggroup.add_argument('-d', '--debug', 		action='store_true', default=False, help='Debug mode')
	arggroup.add_argument('-v', '--verbose', 	action='store_true', default=False, help='Verbose mode')
	arggroup.add_argument('-s', '--silent', 	action='store_true', default=False, help='Silent mode')
	parser.add_argument('-l', '--log', action='store_true', default=False, help='Enable file logging')
	parser.add_argument('--force', action='store_true', default=False, help='Force action')
	parser.add_argument('--workers', type=int, default=N_WORKERS, help='Number of asynchronous workers')

	subparsers = parser.add_subparsers(title='Actions', dest='action', metavar='fetch | update | prune | export', help='action help')
	parser_fetch = subparsers.add_parser('fetch', help='fetch help')
	parser_update = subparsers.add_parser('update', help='fetch help')
	parser_prune = subparsers.add_parser('prune', help='prune help')
	parser_export = subparsers.add_parser('export', help='fetch help')

	subparsers_fetch 				= parser_fetch.add_subparsers(title='Modes', dest='mode', metavar='account_ids | tank_stats | player_achievements', help='action help')
	parser_fetch_account_ids 		= subparsers_fetch.add_parser('account_ids', help='fetch account_ids help')
	parser_fetch_tank_stats 		= subparsers_fetch.add_parser('tank_stats', help='fetch tank_stats help')
	parser_fetch_player_achievements= subparsers_fetch.add_parser('player_achievements', help='fetch player_achievements help')
	
	parser_fetch_account_ids.add_argument('source', metavar='wi | db | replays', type=str, choices=['wi', 'db', 'replays'] , default='wi')
	parser_fetch_account_ids.add_argument('--max', '--max_pages', dest='max_pages', type=int, default=MAX_PAGES, help='Maximum number of WoTinspector.com pages to spider')
	parser_fetch_account_ids.add_argument('--start', '--start_page', dest='start_page', type=int, default=0, help='Start page to start spidering of WoTinspector.com')
	parser_fetch_account_ids.add_argument('replays', metavar='REPLAY1 [REPLAY ...]', type=str, nargs='*', help='Replay files to read. Use \'-\' for STDIN')
	parser_fetch_account_ids.set_defaults(func=fetch_account_ids)

	parser_fetch_tank_stats.add_argument('--cache-valid', type=int, dest='cache_valid', default=CACHE_VALID, help='Do not update stats newer than N Days')
	parser_fetch_tank_stats.add_argument('--player-src', dest='player_src', default='db', choices=[ 'db', 'file' ], help='Source for the account list. Default: db')
	parser_fetch_tank_stats.add_argument('--sample', type=float, default=0, help='Sample size of accounts to update')
	parser_fetch_tank_stats.add_argument('--run-error-log', dest='run_error_log', action='store_true', default=False, help='Re-try previously failed requests and clear errorlog')
	parser_fetch_tank_stats.add_argument('--check-invalid', dest='chk_invalid', action='store_true', default=False, help='Re-check invalid accounts')
	parser_fetch_tank_stats.add_argument('--check-inactive', dest='chk_inactive', action='store_true', default=False, help='Re-check inactive accounts')
	parser_fetch_tank_stats.add_argument('--file', default=None, help='File to read')
	parser_fetch_tank_stats.set_defaults(func=fetch_tank_stats)

	parser.add_argument('-r', '--remove', 		action='store_true', default=False, help='REMOVE account_ids the database. Please give a plain text file with account_ids as cmd line argument.')
	parser.add_argument('files', metavar='FILE1 [FILE2 ...]', type=str, nargs='*', help='Files to read. Use \'-\' for STDIN')

	args = parser.parse_args()
	bu.set_log_level(args.silent, args.verbose, args.debug)
	bu.set_progress_step(100)
	
	players = set()
	try:
		


		
		if not args.remove:
			if args.blitzstars:
				players.update(await get_players_BS(args.force))
		
			if args.wotinspector:
				players.update(await get_players_WI(db, args))

			if args.db:
				players.update(await get_players_DB(db, args))
			
			if len(args.files) > 0:
				bu.debug(str(args.files))
				players.update(await get_players_replays(args.files, args.workers))

			await update_account_ids(db, players)		
		else:
			await invalidate_account_ids(db, args.files)

	except asyncio.CancelledError as err:
		bu.error('Queue gets cancelled while still working.')
	except Exception as err:
		bu.error('Unexpected Exception: ' + str(type(err)) + ' : '+ str(err))
	finally:
		await bs.close()

	return None


async def invalidate_account_ids(db: motor.motor_asyncio.AsyncIOMotorDatabase, files: list):
	"""Invalidate account_ids from the DB to prevent further requests"""
	
	bu.verbose_std('INVALIDATING ACCOUNT_IDs FROM THE DATABASE * * * * * * * * * * * ')
	await asyncio.sleep(5)
	
	account_ids = set()
	for file in files:
		account_ids.update(await bu.read_int_list(file))

	bu.verbose_std('Invalidating ' + str(len(account_ids)) + ' account_ids')
	invalidated = 0
	
	for account_id in account_ids:
		try:
			# mark the account invalid to prevent further requests
			await set_account_invalid(db, account_id)
			# manually check data in the DB			
			invalidated += 1 
		except Exception as err:
			bu.error('Unexpected Exception: ' + str(type(err)) + ' : '+ str(err))
		
	bu.verbose_std(str(invalidated) + ' account_ids invalidated from the database')

	return None


async def set_account_invalid(db: motor.motor_asyncio.AsyncIOMotorDatabase, account_id: int):
	"""Set account_id invalid"""
	dbc = db[su.DB_C_ACCOUNTS]
	try: 
		await dbc.update_one({ '_id': account_id }, { '$set': {'invalid': True }} )		
	except Exception as err:
		bu.error('account_id=' + str(account_id) +  'Unexpected error', exception=err)
	finally:
		bu.debug('account_id=' + str(account_id) + ' Marked as invalid')
	return None


async def write_account_ids2db(db: motor.motor_asyncio.AsyncIOMotorDatabase, account_ids: set):
		"""Update / add account_ids to the database"""
		dbc = db[su.DB_C_ACCOUNTS]
				
		player_list = list()
		for account_id in account_ids:
			if account_id < WG.ACCOUNT_ID_MAX:
				player_list.append(mk_player_JSON(account_id))
		
		count_old = await dbc.count_documents({})
		BATCH = 500
		while len(player_list) > 0:
			if len(player_list) >= BATCH:
				end = BATCH
			else:
				end = len(player_list)
			try:
				bu.print_progress()
				bu.debug('Inserting account_ids to DB')
				await dbc.insert_many(player_list[:end], ordered=False)
			except pymongo.errors.BulkWriteError as err:
				pass
			except Exception as err:
				bu.error('Unexpected Exception: ' + str(type(err)) + ' : ' + str(err))
			del(player_list[:end])

		count = await dbc.count_documents({})
		bu.print_new_line()
		bu.verbose_std(str(count - count_old) + ' new account_ids added to the database')
		bu.verbose_std(str(count) + ' account_ids in the database')

def mk_player_JSON(account_id: int): 
	player = {}
	player['_id'] = account_id
	player['updated'] = bu.NOW()
	player['last_battle_time'] = None
	return player

async def fetch_account_ids(args: argparse.Namespace):
	account_ids = set()
	try:
		if args.source == 'wi':
			account_ids.update(await fetch_account_ids_WI(db, args))
		elif args.source == 'db':
			account_ids.update(await fetch_account_ids_DB(db, args))
		elif args.source == 'replays':
			if len(args.replays) > 0:
				bu.debug(str(args.files))
				account_ids.update(await fetch_account_ids_replays(args))

		await write_account_ids2db(db, account_ids)
	except Exception as err:
		bu.error('Unexpected error: ' + str(type(err)) + ' : ' + str(err))


async def fetch_account_ids_WI(db : motor.motor_asyncio.AsyncIOMotorDatabase, args: argparse.Namespace):
	"""Get active players from wotinspector.com replays"""
	global wi

	workers 	= args.workers
	max_pages 	= args.max_pages
	start_page 	= args.start_page
	force 		= args.force
	account_ids = set()
	replayQ 	= asyncio.Queue(maxsize=200)
	wi 			= WoTinspector(rate_limit=15)
	
	# Start tasks to process the Queue
	tasks = []

	for i in range(workers):
		tasks.append(asyncio.create_task(WI_replay_fetcher(db, replayQ, i, force)))
		bu.debug('Replay Fetcher ' + str(i) + ' started')

	bu.set_progress_bar('Spidering replays', max_pages, step = 1, id = "spider")		

	for page in range(start_page,(start_page + max_pages)):
		if WI_STOP_SPIDER: 
			bu.debug('Stopping spidering WoTispector.com')
			# await empty_queue(replayQ, 'Replay Queue')
			break
		# url = wi.get_url_replay_listing(page)
		bu.print_progress(id = "spider")
		try:
			resp = await wi.get_replay_listing(page)
			if resp.status != 200:
				bu.error('Could not retrieve wotinspector.com')
				continue	
			bu.debug('HTTP request OK')
			html = await resp.text()
			links = wi.get_replay_links(html)
			if len(links) == 0: 
				break
			for link in links:
				await replayQ.put(link)
			# await asyncio.sleep(SLEEP)
		except aiohttp.ClientError as err:
			bu.error("Could not retrieve replays.WoTinspector.com page " + str(page))
			bu.error(str(err))
	
	n_replays = replayQ.qsize()
	bu.set_progress_bar('Fetching replays', n_replays, step = 5, id = 'replays')

	bu.debug('Replay links read. Replay Fetchers to finish')
	await replayQ.join()
	bu.finish_progress_bar()
	bu.debug('Replays fetched. Cancelling fetcher workers')
	for task in tasks:
		task.cancel()
	replays = 0	
	for res in await asyncio.gather(*tasks):
		account_ids.update(res[0])
		replays += res[1]		
	await wi.close()
	bu.verbose_std('Replays added into DB: ' + str(replays))
	return account_ids


async def fetch_account_ids_DB(db, args: argparse.Namespace):
	dbc = db[su.DB_C_REPLAYS]
	account_ids = set()

	cursor = dbc.find({}, { 'data.summary.allies' : 1, 'data.summary.enemies' : 1, '_id' : 0 } )
	async for replay in cursor:
		bu.print_progress()
		
		try:
			account_ids.update(replay['data']['summary']['allies'])
			account_ids.update(replay['data']['summary']['enemies'])			
		except Exception as err:
			bu.error('Unexpected error: ' + str(type(err)) + ' : ' + str(err))
	return account_ids


async def fetch_account_ids_replays(args: argparse.Namespace):
	#files : list, workers: int):
	replays = args.replays
	workers = args.workers
	replayQ  = asyncio.Queue()	
	reader_tasks = []
	# Make replay Queue
	scanner_task = asyncio.create_task(mk_replayQ(replayQ, replays))

	# Start tasks to process the Queue
	for i in range(workers):
		reader_tasks.append(asyncio.create_task(replay_reader(replayQ, i)))
		bu.debug('Task ' + str(i) + ' started')

	bu.debug('Waiting for the replay scanner to finish')
	await asyncio.wait([scanner_task])
	bu.debug('Scanner finished. Waiting for replay readers to finish the queue')
	await replayQ.join()
	bu.debug('Replays read. Cancelling Readers and analyzing results')
	for task in reader_tasks:
		task.cancel()	
	account_ids = set()
	for res in await asyncio.gather(*reader_tasks):
		account_ids.update(res)
	
	return account_ids


async def replay_reader(queue: asyncio.Queue, readerID: int):
	"""Async Worker to process the replay queue"""

	players = set()
	try:
		while True:
			item = await queue.get()
			filename = item[0]
			replayID = item[1]

			try:
				if os.path.exists(filename) and os.path.isfile(filename):
					async with aiofiles.open(filename) as fp:
						replay_json = json.loads(await fp.read())
						tmp = await parse_account_ids(replay_json)
						if tmp != None:
							players.update(tmp)
						else:
							bu.error('Replay[' + str(replayID) + ']: ' + filename + ' is invalid. Skipping.' )
			except Exception as err:
				bu.error(str(err))
			bu.debug('Marking task ' + str(replayID) + ' done')
			queue.task_done()
	except asyncio.CancelledError:		
		return players
	return None

async def parse_account_ids(replay_json: dict): 
	players = set()
	try:
		if not wi.chk_JSON_replay(replay_json):
			raise Exception('Replay file is invalid')
		players.update(replay_json['data']['summary']['allies'])
		players.update(replay_json['data']['summary']['enemies'])	
	except Exception as err:
		bu.error(str(err))
		return None
	return players

async def mk_replayQ(queue : asyncio.Queue, files : list):
	"""Create queue of replays to post"""
	p_replayfile = re.compile('.*\\.wotbreplay\\.json$')

	if files[0] == '-':
		bu.debug('reading replay file list from STDIN')
		stdin, _ = await aioconsole.get_standard_streams()
		while True:
			line = (await stdin.readline()).decode('utf-8').rstrip()
			if not line: 
				break
			else:
				if (p_replayfile.match(line) != None):
					await queue.put(await mk_replayQ_item(line))
	else:
		for fn in files:
			if fn.endswith('"'):
				fn = fn[:-1]  
			if os.path.isfile(fn) and (p_replayfile.match(fn) != None):
				await queue.put(await mk_replayQ_item(fn))
			elif os.path.isdir(fn):
				with os.scandir(fn) as dirEntry:
					for entry in dirEntry:
						if entry.is_file() and (p_replayfile.match(entry.name) != None): 
							bu.debug(entry.name)
							await queue.put(await mk_replayQ_item(entry.path))
			bu.debug('File added to queue: ' + fn)
	bu.debug('Finished')
	return None

async def mk_replayQ_item(filename : str) -> list:
	"""Make an item to replay queue"""
	global REPLAY_N
	REPLAY_N +=1
	bu.print_progress()
	return [filename, REPLAY_N]


async def WI_old_replay_found():
	global WI_old_replay_N, WI_STOP_SPIDER
	WI_old_replay_N +=1
	if WI_old_replay_N == WI_old_replay_limit:
		bu.verbose_std("\n" + str(WI_old_replay_N) + ' old replays spidered. Stopping spidering.')
		WI_STOP_SPIDER = True		 
		return True
	return False

async def WI_replay_fetcher(db : motor.motor_asyncio.AsyncIOMotorDatabase, queue : asyncio.Queue, workerID : int, force : bool):
	account_ids = set()
	dbc = db[su.DB_C_REPLAYS]

	replays = 0 
	while True:
		try:
			replay_link = None
			replay_link = await queue.get()
			bu.print_progress(id = 'replays')
			replay_id = wi.get_replay_id(replay_link)
			res = await dbc.find_one({'_id': replay_id})
			if res != None:
				bu.debug('Replay already in the DB: ' + str(replay_id) , id=workerID)
				if force:
					continue
				else: 
					await WI_old_replay_found()					
					continue
			url = wi.get_url_replay_view(replay_id)
			json_resp = await wi.get_replay_JSON(replay_id)
			
			if json_resp == None:
				bu.debug('Could not fetch valid Replay JSON: ' + url, id=workerID)
				continue
			json_resp['_id'] = replay_id
			try:
				await dbc.insert_one(json_resp)
				replays += 1
				bu.debug('Replay added to database', id=workerID)
			except Exception as err:
				bu.error('Unexpected Exception', exception=err, id=workerID) 
			account_ids.update(await parse_account_ids(json_resp))
			bu.debug('Processed replay: ' + url , id=workerID)
			await asyncio.sleep(SLEEP)
		except asyncio.CancelledError:
			break
		except Exception as err:
			bu.error('Unexpected Exception', exception=err, id=workerID) 
		finally:
			if replay_link != None:
				queue.task_done()	
	return account_ids, replays
	

async def empty_queue(queue : asyncio.Queue, Qname = ''):
	"""Empty the task queue"""
	global WI_STOP_SPIDER
	try:
		bu.debug('Emptying queue: ' + Qname)
		WI_STOP_SPIDER = True
		while True:
			queue.get_nowait()
			queue.task_done()
	except asyncio.QueueEmpty:
		bu.debug('Queue empty: ' + Qname)
	return None

async def mk_playerQ(queue : asyncio.Queue, account_id_list : list):
	"""Create queue of replays to post"""
	for account_id in account_id_list:
		bu.debug('Adding account_id: ' + str(account_id) + ' to the queue')
		await queue.put(account_id)

	return None


    ### main()
if __name__ == "__main__":
   #asyncio.run(main(sys.argv[1:]), debug=True)
   asyncio.run(main(sys.argv[1:]))
