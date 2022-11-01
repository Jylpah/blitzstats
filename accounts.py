from argparse import ArgumentParser, Namespace
from configparser import ConfigParser
from typing import Optional
import logging

from backend import Backend
from models import WoTBlitzReplayJSON

logger = logging.getLogger()
error 	= logger.error
message	= logger.warning
verbose	= logger.info
debug	= logger.debug

WI_MAX_PAGES 	: int 				= 100
WI_RATE_LIMIT	: Optional[float] 	= None
WI_AUTH_TOKEN	: Optional[str] 	= None

###########################################
# 
# add_args_accouts functions  
#
###########################################

def add_args_accounts(parser: ArgumentParser, config: Optional[ConfigParser] = None) -> bool:
	try:
		
		accounts_parsers = parser.add_subparsers(dest='accounts_cmd', 	
												title='accounts commands',
												description='valid commands',
												metavar='')
		
		fetch_parser = accounts_parsers.add_parser('fetch', aliases=['get'], help="accounts fetch help")
		if not add_args_accounts_fetch(fetch_parser, config=config):
			raise Exception("Failed to define argument parser for: accounts fetch")
		
		export_parser = accounts_parsers.add_parser('export', help="accounts export help")
		if not add_args_accounts_export(export_parser, config=config):
			raise Exception("Failed to define argument parser for: accounts export")
		
		remove_parser = accounts_parsers.add_parser('remove', aliases=['rm'], help="accounts remove help")
		if not add_args_accounts_remove(remove_parser, config=config):
			raise Exception("Failed to define argument parser for: accounts remove")
		
		return True
	except Exception as err:
		error(str(err))
	return False


def add_args_accounts_fetch(parser: ArgumentParser, config: Optional[ConfigParser] = None) -> bool:
	try:
		accounts_fetch_parsers = parser.add_subparsers(dest='accounts_fetch_source', 	
														title='accounts fecth source',
														description='valid sources', 
														metavar='')

		accounts_fetch_wi_parser = accounts_fetch_parsers.add_parser('wi', help='accounts fetch wi help')
		if not add_args_accounts_fetch_wi(accounts_fetch_wi_parser, config=config):
			raise Exception("Failed to define argument parser for: accounts fetch wi")
		
		accounts_fetch_files_parser = accounts_fetch_parsers.add_parser('files', help='accounts fetch files help')
		if not add_args_accounts_fetch_files(accounts_fetch_files_parser, config=config):
			raise Exception("Failed to define argument parser for: accounts fetch files")		
		
		return True	
	except Exception as err:
		error(str(err))
	return False


def add_args_accounts_fetch_wi(parser: ArgumentParser, config: Optional[ConfigParser] = None) -> bool:
	try:
		WI_RATE_LIMIT 	: float  		= 20/3600
		WI_MAX_PAGES 	: int 			= 100
		WI_AUTH_TOKEN 	: Optional[str] = None

		if config is not None and 'WI' in config.sections():
			configWI 		= config['WI']
			WI_RATE_LIMIT	= configWI.getfloat('rate_limit', WI_RATE_LIMIT)
			WI_MAX_PAGES	= configWI.getint('max_pages', WI_MAX_PAGES)
			WI_AUTH_TOKEN	= configWI.get('auth_token', WI_AUTH_TOKEN)

		parser.add_argument('--max', '--max-pages', dest='wi_max_pages', 
							type=int, default=WI_MAX_PAGES, metavar='MAX_PAGES',
							help='Maximum number of pages to spider')
		parser.add_argument('--start','--start_page',   dest='wi_start_page', 
							metavar='START_PAGE', type=int, default=0, 
							help='Start page to start spidering of WoTinspector.com')
		parser.add_argument('--wi-auth-token', dest='wi_auth_token', 
							type=int, default=WI_AUTH_TOKEN, metavar='AUTH_TOKEN',
							help='Start page to start spidering of WoTinspector.com')
		parser.add_argument('--wi-rate-limit', type=float, default=WI_RATE_LIMIT, metavar='RATE_LIMIT',
							help='Rate limit for WoTinspector.com')
		
		return True	
	except Exception as err:
		error(str(err))
	return False


def add_args_accounts_fetch_files(parser: ArgumentParser, config: Optional[ConfigParser] = None) -> bool:
	try:
		IMPORT_FORMAT 	= 'txt'

		if config is not None and 'ACCOUNTS' in config.sections():
			configAccs 	= config['ACCOUNTS']
			IMPORT_FORMAT	= configAccs.get('import_format', IMPORT_FORMAT)

		parser.add_argument('--format', type=str, choices=['json', 'txt', 'csv', 'auto'], 
							default=IMPORT_FORMAT, help='Accounts list file format')
		parser.add_argument('files', metavar='FILE1 [FILE2 ...]', type=str, nargs='*', 
							default='-', help='Files to read. Use \'-\' for STDIN')		
		return True	
	except Exception as err:
		error(str(err))
	return False


def add_args_accounts_export(parser: ArgumentParser, config: Optional[ConfigParser] = None) -> bool:
	try:
		EXPORT_FORMAT 	= 'txt'
		EXPORT_FILE 	= 'accounts_export.txt'

		if config is not None and 'ACCOUNTS' in config.sections():
			configAccs 	= config['ACCOUNTS']
			EXPORT_FORMAT	= configAccs.get('export_format', EXPORT_FORMAT)
			EXPORT_FILE		= configAccs.get('export_file', EXPORT_FILE )

		parser.add_argument('--format', type=str, choices=['json', 'txt', 'csv'], 
							default=EXPORT_FORMAT, help='Accounts list file format')
		parser.add_argument('file', metavar='FILE', type=str, nargs=1, default=EXPORT_FILE, 
							help='File to export accounts to. Use \'-\' for STDIN')	
		return True	
	except Exception as err:
		error(str(err))
	return False


def add_args_accounts_remove(parser: ArgumentParser, config: Optional[ConfigParser] = None) -> bool:
	try:
		IMPORT_FORMAT 	= 'txt'

		if config is not None:
			if 'ACCOUNTS' in config.sections():
				configAccs 	= config['ACCOUNTS']
				IMPORT_FORMAT	= configAccs.get('import_format', IMPORT_FORMAT)
					
		parser.add_argument('--format', type=str, choices=['json', 'txt', 'csv'], 
							default=IMPORT_FORMAT, help='Accounts list file format')
		account_src_parser = parser.add_mutually_exclusive_group()
		account_src_parser.add_argument('--file', metavar='FILE', type=str, default=None, 
										help='File to export accounts to. Use \'-\' for STDIN')	
		account_src_parser.add_argument('--accounts', metavar='ACCOUNT_ID [ACCOUNT_ID ...]', type=int, nargs='+', 
										help='accounts to remove')	
		
		return True	
	except Exception as err:
		error(str(err))
	return False

###########################################
# 
# cmd_accouts functions  
#
###########################################

async def cmd_accounts(db: Backend, args : Namespace, config: Optional[ConfigParser] = None) -> bool:
	try:
		debug('accounts')
		replay : WoTBlitzReplayJSON | None = await db.replay_get('1b46e2a1c4432074386f55881892921d')
		print(str(replay))

		if args.accounts_cmd == 'fetch':
			await cmd_accounts_fetch(args, config)

		elif args.accounts_cmd == 'export':
			await cmd_accounts_export(args, config)

		elif args.accounts_cmd == 'remove':
			await cmd_accounts_remove(args, config)

	except Exception as err:
		error(str(err))
	return False


async def cmd_accounts_fetch(args : Namespace, config: Optional[ConfigParser] = None) -> bool:
	try:
		debug('starting')
		
		# init backend connection
		# start backend feeder + queue
		# start source process: wi or files
		# close backend + queue
		# print stats

		if args.accounts_fetch_source == 'wi':
			debug('wi')	
		elif args.accounts_fetch_source == 'files':
			debug('files')

	except Exception as err:
		error(str(err))
	return False


async def cmd_accounts_fetch_files(args : Namespace, config: Optional[ConfigParser] = None) -> bool:
	try:
		debug('starting')
		


	except Exception as err:
		error(str(err))
	return False


async def cmd_accounts_fetch_wi	(args : Namespace, config: Optional[ConfigParser] = None) -> bool:
	try:
		debug('starting')
		
		

	except Exception as err:
		error(str(err))
	return False


async def cmd_accounts_export(args : Namespace, config: Optional[ConfigParser] = None) -> bool:
	try:
		debug('starting')
		if args.accounts_cmd == 'fetch':
			debug('fetch')	
		elif args.accounts_cmd == 'export':
			debug('export')
		elif args.accounts_cmd == 'remove':
			debug('remove')

	except Exception as err:
		error(str(err))
	return False


async def cmd_accounts_remove(args : Namespace, config: Optional[ConfigParser] = None) -> bool:
	try:
		debug('starting')
		if args.accounts_cmd == 'fetch':
			debug('fetch')	
		elif args.accounts_cmd == 'export':
			debug('export')
		elif args.accounts_cmd == 'remove':
			debug('remove')

	except Exception as err:
		error(str(err))
	return False