from __future__ import absolute_import, division, print_function

import io
import itertools
import math
import bz2
import os
import uuid
from fnmatch import fnmatchcase
from glob import glob
from collections import Iterable, Iterator, defaultdict
from functools import wraps, partial

from ..utils import ignoring

from toolz import (merge, frequencies, merge_with, take, reduce,
                   join, reduceby, valmap, count, map, partition_all, filter,
                   remove, pluck, groupby, topk, compose, drop, curry)
import toolz
with ignoring(ImportError):
    from cytoolz import (frequencies, merge_with, join, reduceby,
                         count, pluck, groupby, topk)

from ..base import Base, normalize_token, tokenize
from ..compatibility import (apply, BytesIO, unicode, urlopen, urlparse,
        GzipFile)
from ..core import list2, quote, istask, get_dependencies, reverse_dict
from ..multiprocessing import get as mpget
from ..optimize import fuse, cull, inline
from ..utils import (file_size, infer_compression, open, system_encoding,
                     takes_multiple_arguments, textblock, funcname)

no_default = '__no__default__'


def lazify_task(task, start=True):
    """
    Given a task, remove unnecessary calls to ``list``

    Examples
    --------

    >>> task = (sum, (list, (map, inc, [1, 2, 3])))  # doctest: +SKIP
    >>> lazify_task(task)  # doctest: +SKIP
    (sum, (map, inc, [1, 2, 3]))
    """
    if not istask(task):
        return task
    head, tail = task[0], task[1:]
    if not start and head in (list, reify):
        task = task[1]
        return lazify_task(*tail, start=False)
    else:
        return (head,) + tuple([lazify_task(arg, False) for arg in tail])


def lazify(dsk):
    """
    Remove unnecessary calls to ``list`` in tasks

    See Also
    --------
    ``dask.bag.core.lazify_task``
    """
    return valmap(lazify_task, dsk)


def inline_singleton_lists(dsk):
    """ Inline lists that are only used once

    >>> d = {'b': (list, 'a'),
    ...      'c': (f, 'b', 1)}     # doctest: +SKIP
    >>> inline_singleton_lists(d)  # doctest: +SKIP
    {'c': (f, (list, 'a'), 1)}

    Pairs nicely with lazify afterwards
    """

    dependencies = dict((k, get_dependencies(dsk, k)) for k in dsk)
    dependents = reverse_dict(dependencies)

    keys = [k for k, v in dsk.items()
            if istask(v) and v and v[0] is list and len(dependents[k]) == 1]
    return inline(dsk, keys, inline_constants=False)


def optimize(dsk, keys, **kwargs):
    """ Optimize a dask from a dask.bag """
    dsk2 = cull(dsk, keys)
    dsk3 = fuse(dsk2, keys)
    dsk4 = inline_singleton_lists(dsk3)
    dsk5 = lazify(dsk4)
    return dsk5


def to_textfiles(b, path, name_function=str, compression='infer',
                 encoding=system_encoding):
    """ Write bag to disk, one filename per partition, one line per element

    **Paths**: This will create one file for each partition in your bag. You
    can specify the filenames in a variety of ways.

    Use a globstring

    >>> b.to_textfiles('/path/to/data/*.json.gz')  # doctest: +SKIP

    The * will be replaced by the increasing sequence 1, 2, ...

    ::

        /path/to/data/0.json.gz
        /path/to/data/1.json.gz

    Use a globstring and a ``name_function=`` keyword argument.  The
    name_function function should expect an integer and produce a string.

    >>> from datetime import date, timedelta
    >>> def name(i):
    ...     return str(date(2015, 1, 1) + i * timedelta(days=1))

    >>> name(0)
    '2015-01-01'
    >>> name(15)
    '2015-01-16'

    >>> b.to_textfiles('/path/to/data/*.json.gz', name_function=name)  # doctest: +SKIP

    ::

        /path/to/data/2015-01-01.json.gz
        /path/to/data/2015-01-02.json.gz
        ...

    You can also provide an explicit list of paths.

    >>> paths = ['/path/to/data/alice.json.gz', '/path/to/data/bob.json.gz', ...]  # doctest: +SKIP
    >>> b.to_textfiles(paths) # doctest: +SKIP

    **Compression**: Filenames with extensions corresponding to known
    compression algorithms (gz, bz2) will be compressed accordingly.
    """
    if isinstance(path, (str, unicode)):
        if '*' in path:
            paths = [path.replace('*', name_function(i))
                     for i in range(b.npartitions)]
        else:
            paths = [os.path.join(path, '%s.part' % name_function(i))
                     for i in range(b.npartitions)]
    elif isinstance(path, (tuple, list, set)):
        assert len(path) == b.npartitions
        paths = path
    else:
        raise ValueError("""Path should be either"
1.  A list of paths -- ['foo.json', 'bar.json', ...]
2.  A directory -- 'foo/
3.  A path with a * in it -- 'foo.*.json'""")

    def get_compression(path, compression=compression):
        if compression == 'infer':
            compression = infer_compression(path)
        return compression

    name = 'to-textfiles-' + uuid.uuid4().hex
    dsk = dict(((name, i), (write, (b.name, i), path, get_compression(path),
                            encoding))
               for i, path in enumerate(paths))

    return Bag(merge(b.dask, dsk), name, b.npartitions)


def finalize(results):
    if isinstance(results, Iterator):
        results = list(results)
    if isinstance(results[0], Iterable) and not isinstance(results[0], str):
        results = toolz.concat(results)
    if isinstance(results, Iterator):
        results = list(results)
    return results


def finalize_item(results):
    return results[0]


class Item(Base):
    _optimize = staticmethod(optimize)
    _default_get = staticmethod(mpget)
    _finalize = staticmethod(finalize_item)

    def __init__(self, dsk, key):
        self.dask = dsk
        self.key = key
        self.name = key

    def _keys(self):
        return [self.key]

    def apply(self, func):
        name = 'apply-{0}-{1}'.format(funcname(func), tokenize(self, func))
        dsk = {name: (func, self.key)}
        return Item(merge(self.dask, dsk), name)

    __int__ = __float__ = __complex__ = __bool__ = Base.compute


class Bag(Base):
    """ Parallel collection of Python objects

    Examples
    --------

    Create Bag from sequence

    >>> import dask.bag as db
    >>> b = db.from_sequence(range(5))
    >>> list(b.filter(lambda x: x % 2 == 0).map(lambda x: x * 10))  # doctest: +SKIP
    [0, 20, 40]

    Create Bag from filename or globstring of filenames

    >>> b = db.from_filenames('/path/to/mydata.*.json.gz').map(json.loads)  # doctest: +SKIP

    Create manually (expert use)

    >>> dsk = {('x', 0): (range, 5),
    ...        ('x', 1): (range, 5),
    ...        ('x', 2): (range, 5)}
    >>> b = Bag(dsk, 'x', npartitions=3)

    >>> sorted(b.map(lambda x: x * 10))  # doctest: +SKIP
    [0, 0, 0, 10, 10, 10, 20, 20, 20, 30, 30, 30, 40, 40, 40]

    >>> int(b.fold(lambda x, y: x + y))  # doctest: +SKIP
    30
    """
    _optimize = staticmethod(optimize)
    _default_get = staticmethod(mpget)
    _finalize = staticmethod(finalize)

    def __init__(self, dsk, name, npartitions):
        self.dask = dsk
        self.name = name
        self.npartitions = npartitions
        self.str = StringAccessor(self)

    def map(self, func):
        """ Map a function across all elements in collection

        >>> import dask.bag as db
        >>> b = db.from_sequence(range(5))
        >>> list(b.map(lambda x: x * 10))  # doctest: +SKIP
        [0, 10, 20, 30, 40]
        """
        name = 'map-{0}-{1}'.format(funcname(func), tokenize(self, func))
        if takes_multiple_arguments(func):
            func = partial(apply, func)
        dsk = dict(((name, i), (reify, (map, func, (self.name, i))))
                   for i in range(self.npartitions))
        return type(self)(merge(self.dask, dsk), name, self.npartitions)

    @property
    def _args(self):
        return (self.dask, self.name, self.npartitions)

    def filter(self, predicate):
        """ Filter elements in collection by a predicate function

        >>> def iseven(x):
        ...     return x % 2 == 0

        >>> import dask.bag as db
        >>> b = db.from_sequence(range(5))
        >>> list(b.filter(iseven))  # doctest: +SKIP
        [0, 2, 4]
        """
        name = 'filter-{0}-{1}'.format(funcname(predicate),
                                       tokenize(self, predicate))
        dsk = dict(((name, i), (reify, (filter, predicate, (self.name, i))))
                   for i in range(self.npartitions))
        return type(self)(merge(self.dask, dsk), name, self.npartitions)

    def remove(self, predicate):
        """ Remove elements in collection that match predicate

        >>> def iseven(x):
        ...     return x % 2 == 0

        >>> import dask.bag as db
        >>> b = db.from_sequence(range(5))
        >>> list(b.remove(iseven))  # doctest: +SKIP
        [1, 3]
        """
        name = 'remove-{0}-{1}'.format(funcname(predicate),
                                       tokenize(self, predicate))
        dsk = dict(((name, i), (reify, (remove, predicate, (self.name, i))))
                   for i in range(self.npartitions))
        return type(self)(merge(self.dask, dsk), name, self.npartitions)

    def map_partitions(self, func):
        """ Apply function to every partition within collection

        Note that this requires you to understand how dask.bag partitions your
        data and so is somewhat internal.

        >>> b.map_partitions(myfunc)  # doctest: +SKIP
        """
        name = 'map-partitions-{0}-{1}'.format(funcname(func),
                                               tokenize(self, func))
        dsk = dict(((name, i), (func, (self.name, i)))
                   for i in range(self.npartitions))
        return type(self)(merge(self.dask, dsk), name, self.npartitions)

    def pluck(self, key, default=no_default):
        """ Select item from all tuples/dicts in collection

        >>> b = from_sequence([{'name': 'Alice', 'credits': [1, 2, 3]},
        ...                    {'name': 'Bob',   'credits': [10, 20]}])
        >>> list(b.pluck('name'))  # doctest: +SKIP
        ['Alice', 'Bob']
        >>> list(b.pluck('credits').pluck(0))  # doctest: +SKIP
        [1, 10]
        """
        name = 'pluck-' + tokenize(self, key, default)
        key = quote(key)
        if default == no_default:
            dsk = dict(((name, i), (list, (pluck, key, (self.name, i))))
                       for i in range(self.npartitions))
        else:
            dsk = dict(((name, i), (list, (pluck, key, (self.name, i), default)))
                       for i in range(self.npartitions))
        return type(self)(merge(self.dask, dsk), name, self.npartitions)

    @classmethod
    def from_sequence(cls, *args, **kwargs):
        raise AttributeError("db.Bag.from_sequence is deprecated.\n"
                             "Use db.from_sequence instead.")

    @classmethod
    def from_filenames(cls, *args, **kwargs):
        raise AttributeError("db.Bag.from_filenames is deprecated.\n"
                             "Use db.from_filenames instead.")

    @wraps(to_textfiles)
    def to_textfiles(self, path, name_function=str, compression='infer',
                     encoding=system_encoding):
        return to_textfiles(self, path, name_function, compression, encoding)

    def fold(self, binop, combine=None, initial=no_default, split_every=None):
        """ Parallelizable reduction

        Fold is like the builtin function ``reduce`` except that it works in
        parallel.  Fold takes two binary operator functions, one to reduce each
        partition of our dataset and another to combine results between
        partitions

        1.  ``binop``: Binary operator to reduce within each partition
        2.  ``combine``:  Binary operator to combine results from binop

        Sequentially this would look like the following:

        >>> intermediates = [reduce(binop, part) for part in partitions]  # doctest: +SKIP
        >>> final = reduce(combine, intermediates)  # doctest: +SKIP

        If only one function is given then it is used for both functions
        ``binop`` and ``combine`` as in the following example to compute the
        sum:

        >>> def add(x, y):
        ...     return x + y

        >>> b = from_sequence(range(5))
        >>> b.fold(add).compute()  # doctest: +SKIP
        10

        In full form we provide both binary operators as well as their default
        arguments

        >>> b.fold(binop=add, combine=add, initial=0).compute()  # doctest: +SKIP
        10

        More complex binary operators are also doable

        >>> def add_to_set(acc, x):
        ...     ''' Add new element x to set acc '''
        ...     return acc | set([x])
        >>> b.fold(add_to_set, set.union, initial=set()).compute()  # doctest: +SKIP
        {1, 2, 3, 4, 5}

        See Also
        --------

        Bag.foldby
        """
        token = tokenize(self, binop, combine, initial)
        combine = combine or binop
        a = 'foldbinop-{0}-{1}'.format(funcname(binop), token)
        b = 'foldcombine-{0}-{1}'.format(funcname(combine), token)
        initial = quote(initial)
        if initial is not no_default:
            return self.reduction(curry(_reduce, binop, initial=initial),
                                  curry(_reduce, combine),
                                  split_every=split_every)
        else:
            from toolz.curried import reduce
            return self.reduction(reduce(binop), reduce(combine),
                                  split_every=split_every)

    def frequencies(self, split_every=None):
        """ Count number of occurrences of each distinct element

        >>> b = from_sequence(['Alice', 'Bob', 'Alice'])
        >>> dict(b.frequencies())  # doctest: +SKIP
        {'Alice': 2, 'Bob', 1}
        """
        return self.reduction(compose(list, dictitems, frequencies),
                              merge_frequencies,
                              out_type=Bag, split_every=split_every,
                              name='frequencies')

    def topk(self, k, key=None):
        """ K largest elements in collection

        Optionally ordered by some key function

        >>> b = from_sequence([10, 3, 5, 7, 11, 4])
        >>> list(b.topk(2))  # doctest: +SKIP
        [11, 10]

        >>> list(b.topk(2, lambda x: -x))  # doctest: +SKIP
        [3, 4]
        """
        token = tokenize(self, k, key)
        a = 'topk-a-' + token
        b = 'topk-b-' + token
        if key:
            if callable(key) and takes_multiple_arguments(key):
                key = partial(apply, key)
            func = partial(topk, key=key)
        else:
            func = topk
        dsk = dict(((a, i), (list, (func, k, (self.name, i))))
                   for i in range(self.npartitions))
        dsk2 = {(b, 0): (list, (func, k, (toolz.concat, list(dsk.keys()))))}
        return type(self)(merge(self.dask, dsk, dsk2), b, 1)

    def distinct(self):
        """ Distinct elements of collection

        Unordered without repeats.

        >>> b = from_sequence(['Alice', 'Bob', 'Alice'])
        >>> sorted(b.distinct())
        ['Alice', 'Bob']
        """
        return self.reduction(set, curry(apply, set.union), out_type=Bag,
                name='distinct')

    def reduction(self, perpartition, aggregate, split_every=None,
                  out_type=Item, name=None):
        """ Reduce collection with reduction operators

        Parameters
        ----------
        perpartition: function
            reduction to apply to each partition
        aggregate: function
            reduction to apply to the results of all partitions
        split_every: int (optional)
            Group partitions into groups of this size while performing reduction
            Defaults to 8
        out_type: {Bag, Item}
            The out type of the result, Item if a single element, Bag if a list
            of elements.  Defaults to Item.

        Examples
        --------
        >>> b = from_sequence(range(10))
        >>> b.reduction(sum, sum).compute()
        45
        """
        if split_every is None:
            split_every = 8
        if split_every is False:
            split_every = self.npartitions
        token = tokenize(self, perpartition, aggregate, split_every)
        a = '%s-part-%s' % (name or funcname(perpartition), token)
        dsk = dict(((a, i), (perpartition, (self.name, i)))
                   for i in range(self.npartitions))
        k = self.npartitions
        b = a
        fmt = '%s-aggregate-%s' % (name or funcname(aggregate), token)
        depth = 0
        while k > 1:
            c = fmt + str(depth)
            dsk2 = dict(((c, i), (aggregate, [(b, j) for j in inds]))
                 for i, inds in enumerate(partition_all(split_every, range(k))))
            dsk.update(dsk2)
            k = len(dsk2)
            b = c
            depth += 1

        if out_type is Item:
            dsk[b] = dsk.pop((b, 0))
            return Item(merge(self.dask, dsk), b)
        else:
            return Bag(merge(self.dask, dsk), b, 1)

    @wraps(sum)
    def sum(self, split_every=None):
        return self.reduction(sum, sum, split_every=split_every)

    @wraps(max)
    def max(self, split_every=None):
        return self.reduction(max, max, split_every=split_every)

    @wraps(min)
    def min(self, split_every=None):
        return self.reduction(min, min, split_every=split_every)

    @wraps(any)
    def any(self, split_every=None):
        return self.reduction(any, any, split_every=split_every)

    @wraps(all)
    def all(self, split_every=None):
        return self.reduction(all, all, split_every=split_every)

    def count(self, split_every=None):
        """ Count the number of elements """
        return self.reduction(count, sum, split_every=split_every)

    def mean(self):
        """ Arithmetic mean """
        def mean_chunk(seq):
            total, n = 0.0, 0
            for x in seq:
                total += x
                n += 1
            return total, n

        def mean_aggregate(x):
            totals, counts = list(zip(*x))
            return 1.0 * sum(totals) / sum(counts)

        return self.reduction(mean_chunk, mean_aggregate, split_every=False)

    def var(self, ddof=0):
        """ Variance """
        def var_chunk(seq):
            squares, total, n = 0.0, 0.0, 0
            for x in seq:
                squares += x**2
                total += x
                n += 1
            return squares, total, n

        def var_aggregate(x):
            squares, totals, counts = list(zip(*x))
            x2, x, n = float(sum(squares)), float(sum(totals)), sum(counts)
            result = (x2 / n) - (x / n)**2
            return result * n / (n - ddof)

        return self.reduction(var_chunk, var_aggregate, split_every=False)

    def std(self, ddof=0):
        """ Standard deviation """
        return self.var(ddof=ddof).apply(math.sqrt)

    def join(self, other, on_self, on_other=None):
        """ Join collection with another collection

        Other collection must be an Iterable, and not a Bag.

        >>> people = from_sequence(['Alice', 'Bob', 'Charlie'])
        >>> fruit = ['Apple', 'Apricot', 'Banana']
        >>> list(people.join(fruit, lambda x: x[0]))  # doctest: +SKIP
        [('Apple', 'Alice'), ('Apricot', 'Alice'), ('Banana', 'Bob')]
        """
        assert isinstance(other, Iterable)
        assert not isinstance(other, Bag)
        if on_other is None:
            on_other = on_self
        name = 'join-' + tokenize(self, other, on_self, on_other)
        dsk = dict(((name, i), (list, (join, on_other, other,
                                       on_self, (self.name, i))))
                   for i in range(self.npartitions))
        return type(self)(merge(self.dask, dsk), name, self.npartitions)

    def product(self, other):
        """ Cartesian product between two bags """
        assert isinstance(other, Bag)
        name = 'product-' + tokenize(self, other)
        n, m = self.npartitions, other.npartitions
        dsk = dict(((name, i*m + j),
                   (list, (itertools.product, (self.name, i),
                                              (other.name, j))))
                   for i in range(n) for j in range(m))
        return type(self)(merge(self.dask, other.dask, dsk), name, n*m)

    def foldby(self, key, binop, initial=no_default, combine=None,
               combine_initial=no_default):
        """ Combined reduction and groupby

        Foldby provides a combined groupby and reduce for efficient parallel
        split-apply-combine tasks.

        The computation

        >>> b.foldby(key, binop, init)                        # doctest: +SKIP

        is equivalent to the following:

        >>> def reduction(group):                               # doctest: +SKIP
        ...     return reduce(binop, group, init)               # doctest: +SKIP

        >>> b.groupby(key).map(lambda (k, v): (k, reduction(v)))# doctest: +SKIP

        But uses minimal communication and so is *much* faster.

        >>> b = from_sequence(range(10))
        >>> iseven = lambda x: x % 2 == 0
        >>> add = lambda x, y: x + y
        >>> dict(b.foldby(iseven, add))                         # doctest: +SKIP
        {True: 20, False: 25}

        **Key Function**

        The key function determines how to group the elements in your bag.
        In the common case where your bag holds dictionaries then the key
        function often gets out one of those elements.

        >>> def key(x):
        ...     return x['name']

        This case is so common that it is special cased, and if you provide a
        key that is not a callable function then dask.bag will turn it into one
        automatically.  The following are equivalent:

        >>> b.foldby(lambda x: x['name'], ...)  # doctest: +SKIP
        >>> b.foldby('name', ...)  # doctest: +SKIP

        **Binops**

        It can be tricky to construct the right binary operators to perform
        analytic queries.  The ``foldby`` method accepts two binary operators,
        ``binop`` and ``combine``.  Binary operators two inputs and output must
        have the same type.

        Binop takes a running total and a new element and produces a new total:

        >>> def binop(total, x):
        ...     return total + x['amount']

        Combine takes two totals and combines them:

        >>> def combine(total1, total2):
        ...     return total1 + total2

        Each of these binary operators may have a default first value for
        total, before any other value is seen.  For addition binary operators
        like above this is often ``0`` or the identity element for your
        operation.

        >>> b.foldby('name', binop, 0, combine, 0)  # doctest: +SKIP

        See Also
        --------

        toolz.reduceby
        pyspark.combineByKey
        """
        token = tokenize(self, key, binop, initial, combine, combine_initial)
        a = 'foldby-a-' + token
        b = 'foldby-b-' + token
        if combine is None:
            combine = binop
        if initial is not no_default:
            dsk = dict(((a, i),
                        (reduceby, key, binop, (self.name, i), initial))
                       for i in range(self.npartitions))
        else:
            dsk = dict(((a, i),
                        (reduceby, key, binop, (self.name, i)))
                       for i in range(self.npartitions))

        def combine2(acc, x):
            return combine(acc, x[1])

        if combine_initial is not no_default:
            dsk2 = {(b, 0): (dictitems, (
                                reduceby, 0, combine2, (
                                    toolz.concat, (
                                        map, dictitems, list(dsk.keys()))),
                                combine_initial))}
        else:
            dsk2 = {(b, 0): (dictitems, (
                                merge_with, (partial, reduce, combine),
                                list(dsk.keys())))}
        return type(self)(merge(self.dask, dsk, dsk2), b, 1)

    def take(self, k, compute=True):
        """ Take the first k elements

        Evaluates by default, use ``compute=False`` to avoid computation.
        Only takes from the first partition

        >>> b = from_sequence(range(10))
        >>> b.take(3)  # doctest: +SKIP
        (0, 1, 2)
        """
        name = 'take-' + tokenize(self, k)
        dsk = {(name, 0): (list, (take, k, (self.name, 0)))}
        b = Bag(merge(self.dask, dsk), name, 1)
        if compute:
            return tuple(b.compute())
        else:
            return b

    def _keys(self):
        return [(self.name, i) for i in range(self.npartitions)]

    def concat(self):
        """ Concatenate nested lists into one long list

        >>> b = from_sequence([[1], [2, 3]])
        >>> list(b)
        [[1], [2, 3]]

        >>> list(b.concat())
        [1, 2, 3]
        """
        name = 'concat-' + tokenize(self)
        dsk = dict(((name, i), (list, (toolz.concat, (self.name, i))))
                   for i in range(self.npartitions))
        return type(self)(merge(self.dask, dsk), name, self.npartitions)

    def __iter__(self):
        return iter(self.compute())

    def groupby(self, grouper, npartitions=None, blocksize=2**20):
        """ Group collection by key function

        Note that this requires full dataset read, serialization and shuffle.
        This is expensive.  If possible you should use ``foldby``.

        >>> b = from_sequence(range(10))
        >>> dict(b.groupby(lambda x: x % 2 == 0))  # doctest: +SKIP
        {True: [0, 2, 4, 6, 8], False: [1, 3, 5, 7, 9]}

        See Also
        --------

        Bag.foldby
        """
        if npartitions is None:
            npartitions = self.npartitions
        token = tokenize(self, grouper, npartitions, blocksize)

        import partd
        p = ('partd-' + token,)
        try:
            dsk1 = {p: (partd.Python, (partd.Snappy, partd.File()))}
        except AttributeError:
            dsk1 = {p: (partd.Python, partd.File())}

        # Partition data on disk
        name = 'groupby-part-{0}-{1}'.format(funcname(grouper), token)
        dsk2 = dict(((name, i), (partition, grouper, (self.name, i),
                                 npartitions, p, blocksize))
                    for i in range(self.npartitions))

        # Barrier
        barrier_token = 'groupby-barrier-' + token

        def barrier(args):
            return 0

        dsk3 = {barrier_token: (barrier, list(dsk2))}

        # Collect groups
        name = 'groupby-collect-' + token
        dsk4 = dict(((name, i),
                     (collect, grouper, i, p, barrier_token))
                    for i in range(npartitions))

        return type(self)(merge(self.dask, dsk1, dsk2, dsk3, dsk4), name,
                          npartitions)

    def to_dataframe(self, columns=None):
        """ Convert Bag to dask.dataframe

        Bag should contain tuple or dict records.

        Provide ``columns=`` keyword arg to specify column names.

        Index will not be particularly meaningful.  Use ``reindex`` afterwards
        if necessary.

        Examples
        --------

        >>> import dask.bag as db
        >>> b = db.from_sequence([{'name': 'Alice',   'balance': 100},
        ...                       {'name': 'Bob',     'balance': 200},
        ...                       {'name': 'Charlie', 'balance': 300}],
        ...                      npartitions=2)
        >>> df = b.to_dataframe()

        >>> df.compute()
           balance     name
        0      100    Alice
        1      200      Bob
        0      300  Charlie
        """
        import pandas as pd
        import dask.dataframe as dd
        if columns is None:
            head = self.take(1)[0]
            if isinstance(head, dict):
                columns = sorted(head)
            elif isinstance(head, (tuple, list)):
                columns = list(range(len(head)))
        name = 'to_dataframe-' + tokenize(self, columns)
        DataFrame = partial(pd.DataFrame, columns=columns)
        dsk = dict(((name, i), (DataFrame, (list2, (self.name, i))))
                   for i in range(self.npartitions))

        divisions = [None] * (self.npartitions + 1)

        return dd.DataFrame(merge(optimize(self.dask, self._keys()), dsk),
                            name, columns, divisions)

    def to_imperative(self):
        """ Convert bag to dask Values

        Returns list of values, one value per partition.
        """
        from dask.imperative import Value
        return [Value(k, [self.dask]) for k in self._keys()]

    def repartition(self, npartitions):
        """ Coalesce bag into fewer partitions

        Examples
        --------
        >>> b.repartition(5)  # set to have 5 partitions  # doctest: +SKIP
        """
        if npartitions > self.npartitions:
            raise NotImplementedError(
              "Repartition only supports going to fewer partitions\n"
              " old: %d  new: %d" % (self.npartitions, npartitions))
        size = self.npartitions / npartitions
        L = [int(i * self.npartitions / npartitions)
                for i in range(npartitions + 1)]
        name = 'repartition-%d-%s' % (npartitions, self.name)
        dsk = dict(((name, i), (list,
                                (toolz.concat, [(self.name, j)
                                            for j in range(L[i], L[i + 1])])))
                    for i in range(npartitions))
        return Bag(merge(self.dask, dsk), name, npartitions)


normalize_token.register(Item, lambda a: a.key)
normalize_token.register(Bag, lambda a: a.name)


def partition(grouper, sequence, npartitions, p, nelements=2**20):
    """ Partition a bag along a grouper, store partitions on disk """
    for block in partition_all(nelements, sequence):
        d = groupby(grouper, block)
        d2 = defaultdict(list)
        for k, v in d.items():
            d2[abs(hash(k)) % npartitions].extend(v)
        p.append(d2)
    return p


def collect(grouper, group, p, barrier_token):
    """ Collect partitions from disk and yield k,v group pairs """
    d = groupby(grouper, p.get(group, lock=False))
    return list(d.items())


def from_filenames(filenames, chunkbytes=None, compression='infer',
                   encoding=system_encoding, linesep=os.linesep):
    """ Create dask by loading in lines from many files

    Provide list of filenames

    >>> b = from_filenames(['myfile.1.txt', 'myfile.2.txt'])  # doctest: +SKIP

    Or a globstring

    >>> b = from_filenames('myfiles.*.txt')  # doctest: +SKIP

    Parallelize a large files by providing the number of uncompressed bytes to
    load into each partition.

    >>> b = from_filenames('largefile.txt', chunkbytes=1e7)  # doctest: +SKIP

    See Also
    --------
    from_sequence: A more generic bag creation function
    """
    if isinstance(filenames, str):
        filenames = sorted(glob(filenames))

    if not filenames:
        raise ValueError("No filenames found")

    full_filenames = [os.path.abspath(f) for f in filenames]

    name = 'from-filenames-' + uuid.uuid4().hex

    # Make sure `linesep` is not a byte string because `io.TextIOWrapper` in
    # python versions other than 2.7 dislike byte strings for the `newline`
    # argument.
    linesep = str(linesep)

    def get_compression(path, compression=compression):
        if compression == 'infer':
            compression = infer_compression(path)
        return compression

    if chunkbytes:
        chunkbytes = int(chunkbytes)
        taskss = [_chunk_read_file(fn, chunkbytes, get_compression(fn),
                                   encoding, linesep)
                  for fn in full_filenames]
        d = dict(((name, i), task)
                 for i, task in enumerate(toolz.concat(taskss)))
    else:
        d = dict(((name, i), (list,
                              (io.TextIOWrapper,
                               (io.BufferedReader,
                                (open, fn, 'rb', get_compression(fn))),
                               encoding, None, linesep)))
                 for i, fn in enumerate(full_filenames))

    return Bag(d, name, len(d))


def _chunk_read_file(filename, chunkbytes, compression, encoding, linesep):
    return [(list, (textblock, filename, i, i + chunkbytes, compression,
                    encoding, linesep))
            for i in range(0, file_size(filename, compression), chunkbytes)]


def write(data, filename, compression, encoding):
    dirname = os.path.dirname(filename)
    if not os.path.exists(dirname):
        with ignoring(OSError):
            os.makedirs(dirname)

    f = open(filename, mode='wb', compression=compression)
    try:
        for line in data:
            f.write(line.encode(encoding))
    finally:
        f.close()


def _get_s3_bucket(bucket_name, aws_access_key, aws_secret_key, connection,
                   anon):
    """Connect to s3 and return a bucket"""
    import boto
    if anon is True:
        connection = boto.connect_s3(anon=anon)
    elif connection is None:
        connection = boto.connect_s3(aws_access_key, aws_secret_key)
    return connection.get_bucket(bucket_name)


# we need an unmemoized function to call in the main thread. And memoized
# functions for the dask.
_memoized_get_bucket = toolz.memoize(_get_s3_bucket)


def _get_key(bucket_name, conn_args, key_name):
    bucket = _memoized_get_bucket(bucket_name, *conn_args)
    key = bucket.get_key(key_name)
    ext = key_name.split('.')[-1]
    return stream_decompress(ext, key.read())


def _parse_s3_URI(bucket_name, paths):
    from ..compatibility import quote, unquote
    assert bucket_name.startswith('s3://')
    o = urlparse('s3://' + quote(bucket_name[len('s3://'):]))
    # if path is specified
    if (paths == '*') and (o.path != '' and o.path != '/'):
        paths = unquote(o.path[1:])
    bucket_name = unquote(o.hostname)
    return bucket_name, paths


def from_s3(bucket_name, paths='*', aws_access_key=None, aws_secret_key=None,
            connection=None, anon=False):
    """ Create a Bag by loading textfiles from s3

    Each line will be treated as one element and each file in S3 as one
    partition.

    You may specify a full s3 bucket

    >>> b = from_s3('s3://bucket-name')  # doctest: +SKIP

    Or select files, lists of files, or globstrings of files within that bucket

    >>> b = from_s3('s3://bucket-name', 'myfile.json')  # doctest: +SKIP
    >>> b = from_s3('s3://bucket-name', ['alice.json', 'bob.json'])  # doctest: +SKIP
    >>> b = from_s3('s3://bucket-name', '*.json')  # doctest: +SKIP
    """
    conn_args = (aws_access_key, aws_secret_key, connection, anon)

    bucket_name, paths = normalize_s3_names(bucket_name, paths, conn_args)

    get_key = partial(_get_key, bucket_name, conn_args)

    name = 'from_s3-' + uuid.uuid4().hex
    dsk = dict(((name, i), (list, (get_key, k))) for i, k in enumerate(paths))
    return Bag(dsk, name, len(paths))


def normalize_s3_names(bucket_name, paths, conn_args):
    """ Normalize bucket name and paths """
    if bucket_name.startswith('s3://'):
        bucket_name, paths = _parse_s3_URI(bucket_name, paths)

    if isinstance(paths, str):
        if ('*' not in paths) and ('?' not in paths):
            return bucket_name, [paths]
        else:
            bucket = _get_s3_bucket(bucket_name, *conn_args)
            keys = bucket.list()  # handle globs

            matches = [k.name for k in keys if fnmatchcase(k.name, paths)]
            return bucket_name, matches
    else:
        return bucket_name, paths


def stream_decompress(fmt, data):
    """ Decompress a block of compressed bytes into a stream of strings """
    if fmt == 'gz':
        return GzipFile(fileobj=BytesIO(data))
    if fmt == 'bz2':
        return bz2_stream(data)
    else:
        return map(bytes.decode, BytesIO(data))


def bz2_stream(compressed, chunksize=100000):
    """ Stream lines from a chunk of compressed bz2 data """
    decompressor = bz2.BZ2Decompressor()
    for i in range(0, len(compressed), chunksize):
        chunk = compressed[i: i+chunksize]
        decompressed = decompressor.decompress(chunk).decode()
        for line in decompressed.split('\n'):
            yield line + '\n'


def from_sequence(seq, partition_size=None, npartitions=None):
    """ Create dask from Python sequence

    This sequence should be relatively small in memory.  Dask Bag works
    best when it handles loading your data itself.  Commonly we load a
    sequence of filenames into a Bag and then use ``.map`` to open them.

    Parameters
    ----------

    seq: Iterable
        A sequence of elements to put into the dask
    partition_size: int (optional)
        The length of each partition
    npartitions: int (optional)
        The number of desired partitions

    It is best to provide either ``partition_size`` or ``npartitions``
    (though not both.)

    Examples
    --------

    >>> b = from_sequence(['Alice', 'Bob', 'Chuck'], partition_size=2)

    See Also
    --------
    from_filenames: Specialized bag creation function for textfiles
    """
    seq = list(seq)
    if npartitions and not partition_size:
        partition_size = int(math.ceil(len(seq) / npartitions))
    if npartitions is None and partition_size is None:
        if len(seq) < 100:
            partition_size = 1
        else:
            partition_size = int(len(seq) / 100)

    parts = list(partition_all(partition_size, seq))
    name = 'from_sequence-' + tokenize(seq, partition_size)
    d = dict(((name, i), part) for i, part in enumerate(parts))
    return Bag(d, name, len(d))


def from_castra(x, columns=None, index=False):
    """Load a dask Bag from a Castra.

    Parameters
    ----------
    x : filename or Castra
    columns: list or string, optional
        The columns to load. Default is all columns.
    index: bool, optional
        If True, the index is included as the first element in each tuple.
        Default is False.
    """
    from castra import Castra
    if not isinstance(x, Castra):
        x = Castra(x, readonly=True)
    elif not x._readonly:
        x = Castra(x.path, readonly=True)
    if columns is None:
        columns = x.columns

    name = 'from-castra-' + tokenize(os.path.getmtime(x.path), x.path,
                                     columns, index)
    dsk = dict(((name, i), (load_castra_partition, x, part, columns, index))
                for i, part in enumerate(x.partitions))
    return Bag(dsk, name, len(x.partitions))


def load_castra_partition(castra, part, columns, index):
    import blosc
    # Due to serialization issues, blosc needs to be manually initialized in
    # each process.
    blosc.init()

    df = castra.load_partition(part, columns)
    if isinstance(columns, list):
        items = df.itertuples(index)
    else:
        items = df.iteritems() if index else iter(df)

    items = list(items)
    if (items and isinstance(items[0], tuple)
              and type(items[0]) is not tuple):
        names = items[0]._fields
        items = [dict(zip(names, item)) for item in items]

    return items


def from_url(urls):
    """Create a dask.bag from a url

    >>> a = from_url('http://raw.githubusercontent.com/dask/dask/master/README.rst')  # doctest: +SKIP
    >>> a.npartitions  # doctest: +SKIP
    1

    >> a.take(8)  # doctest: +SKIP
    ('Dask\n',
     '====\n',
     '\n',
     '|Build Status| |Coverage| |Doc Status| |Gitter|\n',
     '\n',
     'Dask provides multi-core execution on larger-than-memory datasets using blocked\n',
     'algorithms and task scheduling.  It maps high-level NumPy and list operations\n',
     'on large datasets on to graphs of many operations on small in-memory datasets.\n')

    >>> b = from_url(['http://github.com', 'http://google.com'])  # doctest: +SKIP
    >>> b.npartitions  # doctest: +SKIP
    2
    """
    if isinstance(urls, str):
        urls = [urls]
    name = 'from_url-' + uuid.uuid4().hex
    dsk = {}
    for i, u in enumerate(urls):
        dsk[(name, i)] = (list, (urlopen, u))
    return Bag(dsk, name, len(urls))


def dictitems(d):
    """ A pickleable version of dict.items

    >>> dictitems({'x': 1})
    [('x', 1)]
    """
    return list(d.items())


def concat(bags):
    """ Concatenate many bags together, unioning all elements

    >>> import dask.bag as db
    >>> a = db.from_sequence([1, 2, 3])
    >>> b = db.from_sequence([4, 5, 6])
    >>> c = db.concat([a, b])

    >>> list(c)
    [1, 2, 3, 4, 5, 6]
    """
    name = 'concat-' + tokenize(*bags)
    counter = itertools.count(0)
    dsk = dict(((name, next(counter)), key)
               for bag in bags for key in sorted(bag._keys()))
    return Bag(merge(dsk, *[b.dask for b in bags]), name, len(dsk))


class StringAccessor(object):
    """ String processing functions

    Examples
    --------

    >>> import dask.bag as db
    >>> b = db.from_sequence(['Alice Smith', 'Bob Jones', 'Charlie Smith'])
    >>> list(b.str.lower())
    ['alice smith', 'bob jones', 'charlie smith']

    >>> list(b.str.match('*Smith'))
    ['Alice Smith', 'Charlie Smith']

    >>> list(b.str.split(' '))
    [['Alice', 'Smith'], ['Bob', 'Jones'], ['Charlie', 'Smith']]
    """
    def __init__(self, bag):
        self._bag = bag

    def __dir__(self):
        return sorted(set(dir(type(self)) + dir(str)))

    def _strmap(self, key, *args, **kwargs):
        return self._bag.map(lambda s: getattr(s, key)(*args, **kwargs))

    def __getattr__(self, key):
        try:
            return object.__getattribute__(self, key)
        except AttributeError:
            if key in dir(str):
                func = getattr(str, key)
                return robust_wraps(func)(partial(self._strmap, key))
            else:
                raise

    def match(self, pattern):
        """ Filter strings by those that match a pattern

        Examples
        --------

        >>> import dask.bag as db
        >>> b = db.from_sequence(['Alice Smith', 'Bob Jones', 'Charlie Smith'])
        >>> list(b.str.match('*Smith'))
        ['Alice Smith', 'Charlie Smith']

        See Also
        --------
        fnmatch.fnmatch
        """
        from fnmatch import fnmatch
        return self._bag.filter(partial(fnmatch, pat=pattern))


def robust_wraps(wrapper):
    """ A weak version of wraps that only copies doc """
    def _(wrapped):
        wrapped.__doc__ = wrapper.__doc__
        return wrapped
    return _


def reify(seq):
    if isinstance(seq, Iterator):
        seq = list(seq)
    if seq and isinstance(seq[0], Iterator):
        seq = list(map(list, seq))
    return seq


def from_imperative(values):
    """ Create bag from many imperative objects

    Parameters
    ----------
    values: list of Values
        An iterable of dask.imperative.Value objects, such as come from dask.do
        These comprise the individual partitions of the resulting bag

    Returns
    -------
    Bag

    Examples
    --------
    >>> b = from_imperative([x, y, z])  # doctest: +SKIP
    """
    from dask.imperative import Value
    if isinstance(values, Value):
        values = [values]
    dsk = merge(v.dask for v in values)

    name = 'bag-from-imperative-' + tokenize(*values)
    names = [(name, i) for i in range(len(values))]
    values = [v.key for v in values]
    dsk2 = dict(zip(names, values))

    return Bag(merge(dsk, dsk2), name, len(values))


def merge_frequencies(seqs):
    return list(merge_with(sum, map(dict, seqs)).items())


def bag_range(n, npartitions):
    """ Numbers from zero to n

    Examples
    --------

    >>> import dask.bag as db
    >>> b = db.range(5, npartitions=2)
    >>> list(b)
    [0, 1, 2, 3, 4]
    """
    size = n // npartitions
    name = 'range-%d-npartitions-%d' % (n, npartitions)
    ijs = list(enumerate(take(npartitions, range(0, n, size))))
    dsk = dict(((name, i), (reify, (range, j, min(j + size, n))))
                for i, j in ijs)

    if n % npartitions != 0:
        i, j = ijs[-1]
        dsk[(name, i)] = (reify, (range, j, n))

    return Bag(dsk, name, npartitions)


def _reduce(binop, sequence, initial=no_default):
    if initial is not no_default:
        return reduce(binop, sequence, initial)
    else:
        return reduce(binop, sequence)
