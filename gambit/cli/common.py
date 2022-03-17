from typing import Optional, Sequence, TextIO, Union, Iterable, Tuple, List
from pathlib import Path

import click
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from gambit.kmers import KmerSpec
from gambit.db import ReferenceDatabase
from gambit.db.models import only_genomeset
from gambit.db.sqla import ReadOnlySession
from gambit.sigs.base import ReferenceSignatures, load_signatures
from gambit.util.io import FilePath, read_lines
from gambit.util.misc import join_list_human


class CLIContext:
	"""Click context object for GAMBIT CLI.

	Loads reference database data lazily the first time it is requested.

	Currently a single option (or environment variable) is used to specify the location of the
	database files, in the future options may be added to specify the reference genomes SQLite
	file and genome signatures file separately. Class methods treat them as being independent.

	Attributes
	----------
	root_context
		Click context object from root command group.
	db_path
		Path to directory containing database files, specified in root command group.
	has_genomes
		Whether reference genome metadata is available.
	has_signatures
		Whether reference signatures are available.
	has_database
		Whether reference genome metadata and reference signatures are both available.
	engine
		SQLAlchemy engine connecting to genomes database.
	Session
		SQLAlchemy session maker for genomes database.
	signatures
		Reference genome signatures.
	"""
	root_context: click.Context
	db_path: Optional[Path]
	has_genomes: bool
	has_signatures: bool
	has_database: bool
	engine: Optional[Engine]
	Session: Optional[sessionmaker]
	signatures: Optional[ReferenceSignatures]

	def __init__(self, root_context: click.Context):
		"""
		Parameters
		----------
		root_context
			Click context object from root command group.
		"""
		self.root_context = root_context

		db_path = root_context.params['db_path']
		self.db_path = None if db_path is None else Path(db_path)

		self._db_found = False
		self._has_genomes = None
		self._has_signatures = None
		self._signatures_path = None

		self._engine = None
		self._Session = None
		self._signatures = None

	def _find_db(self):
		"""Find database files."""
		if self._db_found:
			return

		if self.db_path is None:
			self._has_genomes = self._has_signatures = False

		else:
			self._has_genomes = self._has_signatures = True
			self._genomes_path, self._signatures_path = ReferenceDatabase.locate_files(self.db_path)

		self._db_found = True

	@property
	def has_genomes(self):
		if not self._db_found:
			self._find_db()
		return self._has_genomes

	@property
	def has_signatures(self):
		if not self._db_found:
			self._find_db()
		return self._has_signatures

	@property
	def has_database(self):
		return self.has_genomes and self.has_signatures

	def require_database(self):
		"""Raise an exception if genome metadata and signatures are not available."""
		if not self.has_database:
			raise click.ClickException('Must supply path to database directory.')

	def require_genomes(self):
		"""Raise an exception if genome metadata is not available."""
		self.require_database()

	def require_signatures(self):
		"""Raise an exception if signatures are not available."""
		self.require_database()

	def _init_genomes(self):
		if self._engine is not None or not self.has_genomes:
			return

		self._engine = create_engine(f'sqlite:///{self._genomes_path}')
		self._Session = sessionmaker(self.engine, class_=ReadOnlySession)

	@property
	def engine(self):
		self._init_genomes()
		return self._engine

	@property
	def Session(self):
		self._init_genomes()
		return self._Session

	@property
	def signatures(self):
		if self._signatures is None and self.has_signatures:
			self._signatures = load_signatures(self._signatures_path)

		return self._signatures

	def get_database(self) -> ReferenceDatabase:
		"""Get reference database object."""
		self.require_database()
		session = self.Session()
		gset = only_genomeset(session)
		return ReferenceDatabase(gset, self.signatures)


def filepath(**kw) -> click.Path:
	kw.setdefault('path_type', Path)
	return click.Path(file_okay=True, dir_okay=False, **kw)

def dirpath(**kw) -> click.Path:
	kw.setdefault('path_type', Path)
	return click.Path(file_okay=False, dir_okay=True, **kw)


def genome_files_arg():
	return click.argument(
		'files_arg',
		nargs=-1,
		type=filepath(exists=True),
		metavar='GENOMES...',
	)

def kspec_params(f):
	"""Decorator to add k and prefix options to command."""
	popt = click.option(
		'-p', '--prefix',
		help='K-mer prefix.',
	)
	kopt = click.option(
		'-k',
		type=int,
		help='Number of nucleotides to recognize AFTER prefix.',
	)
	return kopt(popt(f))

def kspec_from_params(k: int, prefix: str) -> Optional[KmerSpec]:

	if prefix is None and k is None:
		return None

	if not (prefix is not None and k is not None):
		raise click.ClickException('Must specify values for both -k and --prefix arguments.')

	return KmerSpec(k, prefix)

def get_sequence_files(explicit: Optional[Iterable[FilePath]]=None,
                       listfile: Union[None, FilePath, TextIO]=None,
                       listfile_dir: Optional[str]=None,
                       ) -> Tuple[Optional[List[str]], Optional[List[Path]]]:
	"""Get list of sequence file paths from several types of CLI arguments.

	Does not check for conflict between ``explicit`` and ``listfile``.

	Parameters
	----------
	explicit
		List of paths given explicitly, such as with a positional argument.
	listfile
		File listing sequence files, one per line.
	listfile_dir
		Parent directory for files in ``listfile``.

	Returns
	-------
	Tuple[Optional[List[str]], Optional[List[Path]]]
		``(ids, paths)`` tuple. ``ids`` is a list of string IDs that can be used to label output.
		If the ``explicit`` and ``listfile`` arguments are None both components of the tuple will be
		None as well.
	"""
	if explicit:
		files = list(map(Path, explicit))
		return list(map(str, files)), files

	elif listfile is not None:
		lines = list(read_lines(listfile, skip_empty=True))
		paths = [Path(listfile_dir) / line for line in lines]
		return lines, paths

	else:
		return None, None


def params_by_name(cmd: click.Command, names: Optional[Iterable[str]]=None):
	"""Get parameters of click command by name.

	Parameters
	----------
	cmd
	names
		Names of specific parameters to get.

	Returns
	-------
	Union[Dict[str, click.Parameter], List[click.Parameter]]
		Parameters with given in ``names`` argument if not None, otherwise a dictionary containing
		all of the command's parameters keyed by name.
	"""
	by_name = {param.name: param for param in cmd.params}
	if names is None:
		return by_name
	else:
		return [by_name[name] for name in names]

def check_params_group(ctx: click.Context, names: Iterable[str], exclusive: bool, required: bool):
	"""Check for the presence of the given parameter values and raise an informative error if needed.

	Parameters
	----------
	ctx
	names
		Parameter names.
	exclusive
		No more than one of the parameters may be present.
	required
		At least one of the parameters must be present.

	Raises
	------
	click.ClickException
	"""
	nfound = sum(bool(ctx.params[name]) for name in names)

	if exclusive and nfound > 1:
		params = params_by_name(ctx.command, names)
		plist = join_list_human(map(param_name_human, params), 'and')
		raise click.ClickException(f'{plist} are mutually exclusive')

	if required and nfound == 0:
		params = params_by_name(ctx.command, names)
		plist = join_list_human(map(param_name_human, params), 'or')
		raise click.ClickException(f'One of {plist} is required')

def param_name_human(param: click.Parameter) -> str:
	"""Get the name/metavar of the given parameter as it appears in the auto-generated help output."""
	if isinstance(param, click.Option):
		# return param.opts[0]
		return '/'.join(param.opts)
	if isinstance(param, click.Argument):
		if param.metavar is not None:
			return param.metavar.rstrip('.')  # Remove ellipsis
		else:
			return param.opts[0].upper()
	raise TypeError(f'Expected click.Parameter, got {type(param)}')


def print_table(rows: Sequence[Sequence], colsep: str=' ', left: str='', right: str=''):
	"""Print a basic table."""

	echo = lambda s: click.echo(s, nl=False)

	rows = [list(map(str, row)) for row in rows]
	ncol = max(map(len, rows))

	widths = [0] * ncol
	for row in rows:
		for i, val in enumerate(row):
			widths[i] = max(widths[i], len(val))

	for row in rows:
		echo(left)

		for i, val in enumerate(row):
			echo(val.ljust(widths[i]))

			if i < ncol - 1:
				echo(colsep)

		echo(right)
		echo('\n')
