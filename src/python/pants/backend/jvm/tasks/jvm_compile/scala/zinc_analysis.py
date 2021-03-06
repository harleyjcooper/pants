# coding=utf-8
# Copyright 2014 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import (absolute_import, division, generators, nested_scopes, print_function,
                        unicode_literals, with_statement)

import itertools
import os
from collections import defaultdict

from twitter.common.collections import OrderedSet

from pants.backend.jvm.tasks.jvm_compile.analysis import Analysis
from pants.backend.jvm.tasks.jvm_compile.scala.zinc_analysis_diff import ZincAnalysisElementDiff
from pants.base.build_environment import get_buildroot


class ZincAnalysisElement(object):
  """Encapsulates one part of a Zinc analysis.

  Zinc analysis files are text files consisting of sections. Each section is introduced by
  a header, followed by lines of the form K -> V, where the same K may repeat multiple times.

  For example, the 'products:' section maps source files to the class files it produces, e.g.,

  products:
  123 items
  org/pantsbuild/Foo.scala -> org/pantsbuild/Foo.class
  org/pantsbuild/Foo.scala -> org/pantsbuild/Foo$.class
  ...

  Related consecutive sections are bundled together in "elements". E.g., the Stamps element
  bundles the section for source file stamps, the section for jar file stamps etc.

  An instance of this class represents such an element.
  """

  # The section names for the sections in this element. Subclasses override.
  headers = ()

  def __init__(self, args):
    # self.args is a list of maps from key to list of values. Each map corresponds to a
    # section in the analysis file. E.g.,
    #
    # 'org/pantsbuild/Foo.scala': ['org/pantsbuild/Foo.class', 'org/pantsbuild/Foo$.class']
    #
    # Subclasses can alias the elements of self.args in their own __init__, for convenience.
    self.args = []
    # Sort the values for each key. This consistency makes it easier to test and to
    # debug live problems in the wild.
    for arg in args:
      sorted_arg = defaultdict(list)
      for k, vs in arg.items():
        sorted_arg[k] = sorted(vs)
      self.args.append(sorted_arg)

  def diff(self, other):
    return ZincAnalysisElementDiff(self, other)

  def __eq__(self, other):
    # Expects keys and vals to be sorted.
    return self.args == other.args

  def __ne__(self, other):
    return not self.__eq__(other)

  def __hash__(self):
    return hash(self.args)

  def write(self, outfile, inline_vals=True, rebasings=None):
    self._write_multiple_sections(outfile, self.headers, self.args, inline_vals, rebasings)

  def _write_multiple_sections(self, outfile, headers, reps, inline_vals=True, rebasings=None):
    """Write multiple sections."""
    for header, rep in zip(headers, reps):
      self._write_section(outfile, header, rep, inline_vals, rebasings)

  def _write_section(self, outfile, header, rep, inline_vals=True, rebasings=None):
    """Write a single section.

    Items are sorted, for ease of testing. TODO: Reconsider this if it hurts performance.
    """
    def rebase(txt):
      for rebase_from, rebase_to in rebasings:
        if rebase_to is None:
          if rebase_from in txt:
            return None
        else:
          txt = txt.replace(rebase_from, rebase_to)
      return txt

    rebasings = rebasings or []
    items = []
    for k, vals in rep.items():
      for v in vals:
        item = rebase('{} -> {}{}'.format(k, '' if inline_vals else '\n', v))
        if item:
          items.append(item)

    items.sort()
    outfile.write(header + ':\n')
    outfile.write('{} items\n'.format(len(items)))
    for item in items:
      outfile.write(item.encode('utf-8'))
      outfile.write('\n')

  def translate_keys(self, token_translator, arg):
    old_keys = list(arg.keys())
    for k in old_keys:
      vals = arg[k]
      del arg[k]
      arg[token_translator.convert(k)] = vals

  def translate_values(self, token_translator, arg):
    for k, vals in arg.iteritems():
      arg[k] = [token_translator.convert(v) for v in vals]

  def translate_base64_values(self, token_translator, arg):
    for k, vals in arg.iteritems():
      arg[k] = [token_translator.convert_base64_string(v) for v in vals]


class ZincAnalysis(Analysis):
  """Parsed representation of a zinc analysis.

  Note also that all files in keys/values are full-path, just as they appear in the analysis file.
  If you want paths relative to the build root or the classes dir or whatever, you must compute
  those yourself.
  """

  # Implementation of class method required by Analysis.

  FORMAT_VERSION_LINE = 'format version: 5\n'

  @staticmethod
  def merge_disjoint_dicts(dicts):
    """Merges multiple dicts with disjoint key sets into one.

    May also be used when we don't care which value is picked for a key that appears more than once.
    """
    ret = defaultdict(list)
    for d in dicts:
      ret.update(d)
    return ret

  @staticmethod
  def merge_overlapping_dicts(dicts):
    """Merges multiple, possibly overlapping, dicts into one.

    If a key exists in more than one dict, takes the largest value in dictionary order.
    This is useful when the values are singleton stamp lists of the form ['lastModified(XXXXXXXX)'],
    as it will lead to taking the most recent modification time.
    """
    ret = defaultdict(list)
    for d in dicts:
      for k, v in d.items():
        if k not in ret or ret[k] < v:
          ret[k] = v
    return ret

  @classmethod
  def merge(cls, analyses):
    # Note: correctly handles "internalizing" external deps that must be internal post-merge.

    # "Merge" compile setup. We assume that all merged analyses have the same setup, so we just take the
    # setup of the first analysis. TODO: Validate that all analyses have the same setup.
    compile_setup = analyses[0].compile_setup if len(analyses) > 0 else CompileSetup((defaultdict(list), ))

    # Merge relations.
    src_prod = ZincAnalysis.merge_disjoint_dicts([a.relations.src_prod for a in analyses])
    binary_dep = ZincAnalysis.merge_disjoint_dicts([a.relations.binary_dep for a in analyses])
    classes = ZincAnalysis.merge_disjoint_dicts([a.relations.classes for a in analyses])
    used = ZincAnalysis.merge_disjoint_dicts([a.relations.used for a in analyses])

    class_to_source = dict((v, k) for k, vs in classes.items() for v in vs)

    def merge_dependencies(internals, externals):
      internal = defaultdict(list)
      external = defaultdict(list)

      naive_internal = ZincAnalysis.merge_disjoint_dicts(internals)
      naive_external = ZincAnalysis.merge_disjoint_dicts(externals)

      # Note that we take care not to create empty values in internal.
      for k, vs in naive_internal.items():
        if vs:
          internal[k].extend(vs)  # Ensure a new list.

      for k, vs in naive_external.items():
        # class->source is many->one, so make sure we only internalize a source once.
        internal_k = OrderedSet(internal.get(k, []))
        for v in vs:
          vfile = class_to_source.get(v)
          if vfile and vfile in src_prod:
            internal_k.add(vfile)  # Internalized.
          else:
            external[k].append(v)  # Remains external.
        if internal_k:
          internal[k] = list(internal_k)
      return internal, external

    internal, external = merge_dependencies(
      [a.relations.internal_src_dep for a in analyses],
      [a.relations.external_dep for a in analyses])

    internal_pi, external_pi = merge_dependencies(
      [a.relations.internal_src_dep_pi for a in analyses],
      [a.relations.external_dep_pi for a in analyses])

    member_ref_internal, member_ref_external = merge_dependencies(
      [a.relations.member_ref_internal_dep for a in analyses],
      [a.relations.member_ref_external_dep for a in analyses])

    inheritance_internal, inheritance_external = merge_dependencies(
      [a.relations.inheritance_internal_dep for a in analyses],
      [a.relations.inheritance_external_dep for a in analyses])

    relations = Relations((src_prod, binary_dep,
                           internal, external,
                           internal_pi, external_pi,
                           member_ref_internal, member_ref_external,
                           inheritance_internal, inheritance_external,
                           classes, used))

    # Merge stamps.
    products = ZincAnalysis.merge_disjoint_dicts([a.stamps.products for a in analyses])
    sources = ZincAnalysis.merge_disjoint_dicts([a.stamps.sources for a in analyses])
    binaries = ZincAnalysis.merge_overlapping_dicts([a.stamps.binaries for a in analyses])
    classnames = ZincAnalysis.merge_disjoint_dicts([a.stamps.classnames for a in analyses])
    stamps = Stamps((products, sources, binaries, classnames))

    # Merge APIs.
    internal_apis = ZincAnalysis.merge_disjoint_dicts([a.apis.internal for a in analyses])
    naive_external_apis = ZincAnalysis.merge_disjoint_dicts([a.apis.external for a in analyses])
    external_apis = defaultdict(list)
    for k, vs in naive_external_apis.items():
      kfile = class_to_source.get(k)
      if kfile and kfile in src_prod:
        internal_apis[kfile] = vs  # Internalized.
      else:
        external_apis[k] = vs  # Remains external.
    apis = APIs((internal_apis, external_apis))

    # Merge source infos.
    source_infos = SourceInfos((ZincAnalysis.merge_disjoint_dicts([a.source_infos.source_infos for a in analyses]), ))

    # Merge compilations.
    compilation_vals = sorted(set([x[0] for a in analyses for x in a.compilations.compilations.itervalues()]))
    compilations_dict = defaultdict(list)
    for i, v in enumerate(compilation_vals):
      compilations_dict['{:03}'.format(int(i))] = [v]
    compilations = Compilations((compilations_dict, ))

    return ZincAnalysis(compile_setup, relations, stamps, apis, source_infos, compilations)

  def __init__(self, compile_setup, relations, stamps, apis, source_infos, compilations):
    (self.compile_setup, self.relations, self.stamps, self.apis, self.source_infos, self.compilations) = \
      (compile_setup, relations, stamps, apis, source_infos, compilations)

  def diff(self, other):
    """Returns a list of element diffs, one per element where self and other differ."""
    element_diffs = []
    for self_elem, other_elem in zip(
            (self.compile_setup, self.relations, self.stamps, self.apis,
             self.source_infos, self.compilations),
            (other.compile_setup, other.relations, other.stamps, other.apis,
             other.source_infos, other.compilations)):
      element_diff = self_elem.diff(other_elem)
      if element_diff.is_different():
        element_diffs.append(element_diff)
    return element_diffs

  def __eq__(self, other):
    return ((self.compile_setup, self.relations, self.stamps, self.apis,
             self.source_infos, self.compilations) ==
            (other.compile_setup, other.relations, other.stamps, other.apis,
             other.source_infos, other.compilations))

  def __ne__(self, other):
    return not self.__eq__(other)

  def __hash__(self):
    return hash((self.compile_setup, self.relations, self.stamps, self.apis,
                 self.source_infos, self.compilations))

  # Implementation of methods required by Analysis.

  def split(self, splits, catchall=False):
    # Note: correctly handles "externalizing" internal deps that must be external post-split.
    buildroot = get_buildroot()
    splits = [set([s if os.path.isabs(s) else os.path.join(buildroot, s) for s in x]) for x in splits]
    if catchall:
      # Even empty sources with no products have stamps.
      remainder_sources = set(self.stamps.sources.keys()).difference(*splits)
      splits.append(remainder_sources)  # The catch-all

    # Split relations.
    src_prod_splits = self._split_dict(self.relations.src_prod, splits)
    binary_dep_splits = self._split_dict(self.relations.binary_dep, splits)
    classes_splits = self._split_dict(self.relations.classes, splits)

    representatives = dict((k, self.representative(k, vs)) for k, vs in self.relations.classes.items())

    def split_dependencies(all_internal, all_external):
      internals = []
      externals = []

      naive_internals = self._split_dict(all_internal, splits)
      naive_externals = self._split_dict(all_external, splits)

      for naive_internal, naive_external, split in zip(naive_internals, naive_externals, splits):
        internal = defaultdict(list)
        external = defaultdict(list)

        # Note that we take care not to create empty values in external.
        for k, vs in naive_external.items():
          if vs:
            external[k].extend(vs)  # Ensure a new list.

        for k, vs in naive_internal.items():
          for v in vs:
            if v in split:
              internal[k].append(v)  # Remains internal.
            else:
              external[k].append(representatives[v])  # Externalized.
        internals.append(internal)
        externals.append(external)
      return internals, externals

    internal_splits, external_splits = \
      split_dependencies(self.relations.internal_src_dep, self.relations.external_dep)
    internal_pi_splits, external_pi_splits = \
      split_dependencies(self.relations.internal_src_dep_pi, self.relations.external_dep_pi)

    member_ref_internal_splits, member_ref_external_splits = \
      split_dependencies(self.relations.member_ref_internal_dep, self.relations.member_ref_external_dep)
    inheritance_internal_splits, inheritance_external_splits = \
      split_dependencies(self.relations.inheritance_internal_dep, self.relations.inheritance_external_dep)
    used_splits = self._split_dict(self.relations.used, splits)

    relations_splits = []
    for args in zip(src_prod_splits, binary_dep_splits,
                    internal_splits, external_splits,
                    internal_pi_splits, external_pi_splits,
                    member_ref_internal_splits, member_ref_external_splits,
                    inheritance_internal_splits, inheritance_external_splits,
                    classes_splits, used_splits):
      relations_splits.append(Relations(args))

    # Split stamps.
    stamps_splits = []
    for src_prod, binary_dep, split in zip(src_prod_splits, binary_dep_splits, splits):
      products_set = set(itertools.chain(*src_prod.values()))
      binaries_set = set(itertools.chain(*binary_dep.values()))
      products = dict((k, v) for k, v in self.stamps.products.items() if k in products_set)
      sources = dict((k, v) for k, v in self.stamps.sources.items() if k in split)
      binaries = dict((k, v) for k, v in self.stamps.binaries.items() if k in binaries_set)
      classnames = dict((k, v) for k, v in self.stamps.classnames.items() if k in binaries_set)
      stamps_splits.append(Stamps((products, sources, binaries, classnames)))

    # Split apis.

    # Externalized deps must copy the target's formerly internal API.
    representative_to_internal_api = {}
    for src, rep in representatives.items():
      representative_to_internal_api[rep] = self.apis.internal.get(src)

    internal_api_splits = self._split_dict(self.apis.internal, splits)

    external_api_splits = []
    for external in external_splits:
      external_api = {}
      for vs in external.values():
        for v in vs:
          if v in representative_to_internal_api:  # This is an externalized dep.
            external_api[v] = representative_to_internal_api[v]
          else: # This is a dep that was already external.
            external_api[v] = self.apis.external[v]
      external_api_splits.append(external_api)

    apis_splits = []
    for args in zip(internal_api_splits, external_api_splits):
      apis_splits.append(APIs(args))

    # Split source infos.
    source_info_splits = \
      [SourceInfos((x, )) for x in self._split_dict(self.source_infos.source_infos, splits)]

    analyses = []
    for relations, stamps, apis, source_infos in zip(relations_splits, stamps_splits, apis_splits, source_info_splits):
      analyses.append(ZincAnalysis(self.compile_setup, relations, stamps, apis, source_infos, self.compilations))

    return analyses

  def write(self, outfile, rebasings=None):
    outfile.write(ZincAnalysis.FORMAT_VERSION_LINE)
    self.compile_setup.write(outfile, inline_vals=True, rebasings=rebasings)
    self.relations.write(outfile, rebasings=rebasings)
    self.stamps.write(outfile, rebasings=rebasings)
    self.apis.write(outfile, inline_vals=False, rebasings=rebasings)
    self.source_infos.write(outfile, inline_vals=False, rebasings=rebasings)
    self.compilations.write(outfile, inline_vals=True, rebasings=rebasings)

  # Extra methods on this class only.

  # Translate the contents of this analysis. Useful for creating anonymized test data.
  # Note that the resulting file is not a valid analysis, as the base64-encoded serialized objects
  # will be replaced with random base64 strings. So these are useful for testing analysis parsing,
  # splitting and merging, but not for actually reading into Zinc.
  def translate(self, token_translator):
    for element in [self.compile_setup, self.relations, self.stamps, self.apis,
                    self.source_infos, self.compilations]:
      element.translate(token_translator)

  def _split_dict(self, d, splits):
    """Split a dict by its keys.

    splits: A list of lists of keys.
    Returns one dict per split.
    """
    ret = []
    for split in splits:
      dict_split = defaultdict(list)
      for f in split:
        if f in d:
          dict_split[f] = d[f]
      ret.append(dict_split)
    return ret

  def representative(self, src, classes):
    """Pick a representative class for each src.

    For historical reasons, external deps are specified as src->class while internal deps are
    specified as src->src.  So when splitting we need to pick a representative.  We must pick
    consistently.
    """
    primary_class_name = os.path.splitext(os.path.basename(src))[0]
    for fqcn in classes:
      if fqcn.rsplit('.', 1)[-1] == primary_class_name:
        # For ease of debugging, pick the class with the same name as the source file, if it exists.
        return fqcn
    # Pick the class that sorts lowest in dictionary order.
    return min(classes)


class CompileSetup(ZincAnalysisElement):
  headers = ('output mode', 'output directories','compile options','javac options',
             'compiler version', 'compile order', 'name hashing')

  def __init__(self, args):
    super(CompileSetup, self).__init__(args)
    (self.output_mode, self.output_dirs, self.compile_options, self.javac_options,
     self.compiler_version, self.compile_order, self.name_hashing) = self.args

  def translate(self, token_translator):
    self.translate_values(token_translator, self.output_dirs)
    for k, vs in list(self.compile_options.items()):  # Make a copy, so we can del as we go.
      # Remove mentions of custom plugins.
      for v in vs:
        if v.startswith('-Xplugin') or v.startswith('-P'):
          del self.compile_options[k]


class Relations(ZincAnalysisElement):
  headers = ('products', 'binary dependencies',
             # TODO: The following 4 headers will go away after SBT completes the
             # transition to the new headers (the 4 after that).
             'direct source dependencies', 'direct external dependencies',
             'public inherited source dependencies', 'public inherited external dependencies',
             'member reference internal dependencies', 'member reference external dependencies',
             'inheritance internal dependencies', 'inheritance external dependencies',
             'class names', 'used names')

  def __init__(self, args):
    super(Relations, self).__init__(args)
    (self.src_prod, self.binary_dep,
     self.internal_src_dep, self.external_dep,
     self.internal_src_dep_pi, self.external_dep_pi,
     self.member_ref_internal_dep, self.member_ref_external_dep,
     self.inheritance_internal_dep, self.inheritance_external_dep,
     self.classes, self.used) = self.args

  def translate(self, token_translator):
    for a in self.args:
      self.translate_values(token_translator, a)
      self.translate_keys(token_translator, a)


class Stamps(ZincAnalysisElement):
  headers = ('product stamps', 'source stamps', 'binary stamps', 'class names')

  def __init__(self, args):
    super(Stamps, self).__init__(args)
    (self.products, self.sources, self.binaries, self.classnames) = self.args

  def translate(self, token_translator):
    for a in self.args:
      self.translate_keys(token_translator, a)
    self.translate_values(token_translator, self.classnames)

  # We make equality ignore the values in classnames: classnames is a map from
  # jar file to one representative class in that jar, and the representative can change.
  # However this doesn't affect any useful aspect of the analysis, so we ignore it.

  def diff(self, other):
    return ZincAnalysisElementDiff(self, other, keys_only_headers=('class names', ))

  def __eq__(self, other):
    return (self.products, self.sources, self.binaries, set(self.classnames.keys())) == \
           (other.products, other.sources, other.binaries, set(other.classnames.keys()))

  def __hash__(self):
    return hash((self.products, self.sources, self.binaries, self.classnames.keys()))


class APIs(ZincAnalysisElement):
  headers = ('internal apis', 'external apis')

  def __init__(self, args):
    super(APIs, self).__init__(args)
    (self.internal, self.external) = self.args

  def translate(self, token_translator):
    for a in self.args:
      self.translate_base64_values(token_translator, a)
      self.translate_keys(token_translator, a)


class SourceInfos(ZincAnalysisElement):
  headers = ("source infos", )

  def __init__(self, args):
    super(SourceInfos, self).__init__(args)
    (self.source_infos, ) = self.args

  def translate(self, token_translator):
    for a in self.args:
      self.translate_base64_values(token_translator, a)
      self.translate_keys(token_translator, a)


class Compilations(ZincAnalysisElement):
  headers = ('compilations', )

  def __init__(self, args):
    super(Compilations, self).__init__(args)
    (self.compilations, ) = self.args
    # Compilations aren't useful and can accumulate to be huge and drag down parse times.
    # We clear them here to prevent them propagating through splits/merges.
    self.compilations.clear()

  def translate(self, token_translator):
    pass
