# ==================================================================================================
# Copyright 2013 Twitter, Inc.
# --------------------------------------------------------------------------------------------------
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this work except in compliance with the License.
# You may obtain a copy of the License in the LICENSE file, or at:
#
#  http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==================================================================================================

import os
import shutil
import textwrap

from contextlib import closing
from xml.etree import ElementTree

from twitter.common.collections import OrderedDict
from twitter.common.contextutil import open_zip as open_jar, temporary_dir
from twitter.common.dirutil import  safe_open

from twitter.pants import get_buildroot
from twitter.pants.base.hash_utils import hash_file
from twitter.pants.goal.workunit import WorkUnit
from twitter.pants.tasks import TaskError
from twitter.pants.binary_util import find_java_home


# Well known metadata file required to register scalac plugins with nsc.
_PLUGIN_INFO_FILE = 'scalac-plugin.xml'

class ZincUtils(object):
  """Convenient wrapper around zinc invocations.

  Instances are immutable, and all methods are reentrant (assuming that the java_runner is).
  """
  def __init__(self, context, nailgun_task, color):
    self.context = context
    self._nailgun_task = nailgun_task  # We run zinc on this task's behalf.
    self._color = color

    self._pants_home = get_buildroot()

    # The target scala version.
    self._compile_profile = context.config.get('scala-compile', 'compile-profile')

    # The zinc version (and the scala version it needs, which may differ from the target version).
    self._zinc_profile = context.config.get('scala-compile', 'zinc-profile')

    # Compiler plugins.
    self._plugins_profile = context.config.get('scala-compile', 'scalac-plugins-profile')

    self._main = context.config.get('scala-compile', 'main')
    self._scalac_args = context.config.getlist('scala-compile', 'args')
    self._jvm_args = context.config.getlist('scala-compile', 'jvm_args')

    if context.options.scala_compile_warnings:
      self._scalac_args.extend(context.config.getlist('scala-compile', 'warning_args'))
    else:
      self._scalac_args.extend(context.config.getlist('scala-compile', 'no_warning_args'))

    cp_for_profile = self._nailgun_task.profile_classpath
    self._zinc_classpath = cp_for_profile(self._zinc_profile)
    self._compiler_classpath = cp_for_profile(self._compile_profile)
    self._plugin_jars = cp_for_profile(self._plugins_profile) if self._plugins_profile else []

    zinc_jars = ZincUtils.identify_zinc_jars(self._zinc_classpath)
    self._zinc_jar_args = []
    self._zinc_jar_args.extend(['-scala-path', ':'.join(self._compiler_classpath)])
    for (name, jarpath) in zinc_jars.items():  # The zinc jar names are also the flag names.
      self._zinc_jar_args.extend(['-%s' % name, jarpath])

    # Allow multiple flags and also comma-separated values in a single flag.
    plugin_names = [p for val in context.options.plugins for p in val.split(',')] \
      if context.options.plugins is not None \
      else context.config.getlist('scala-compile', 'scalac-plugins', default=[])
    plugin_args = context.config.getdict('scala-compile', 'scalac-plugin-args', default={})
    active_plugins = self.find_plugins(plugin_names)

    for name, jar in active_plugins.items():
      self._scalac_args.append('-Xplugin:%s' % jar)
      for arg in plugin_args.get(name, []):
        self._scalac_args.append('-P:%s:%s' % (name, arg))

    # For localizing/relativizing analysis files.
    self._java_home = os.path.realpath(os.path.dirname(find_java_home()))
    self._ivy_home = os.path.realpath(context.config.get('ivy', 'cache_dir'))

  def plugin_jars(self):
    """The jars containing code for enabled plugins."""
    return self._plugin_jars

  def run_zinc(self, args, workunit_name='zinc', workunit_labels=None):
    zinc_args = [
      '-log-level', self.context.options.log_level or 'info',
      '-mirror-analysis',
    ]
    if not self._color:
      zinc_args.append('-no-color')
    zinc_args.extend(self._zinc_jar_args)
    zinc_args.extend(args)
    return self._nailgun_task.runjava_indivisible(self._main, classpath=self._zinc_classpath,
                             args=zinc_args, jvmargs=self._jvm_args, workunit_name=workunit_name,
                             workunit_labels=workunit_labels)

  def compile(self, classpath, sources, output_dir, analysis_file, upstream_analysis_files):
    # To pass options to scalac simply prefix with -S.
    args = ['-S' + x for x in self._scalac_args]

    if len(upstream_analysis_files) > 0:
      args.extend(
        ['-analysis-map', ','.join(['%s:%s' % kv for kv in upstream_analysis_files.items()])])

    args.extend([
      '-analysis-cache', analysis_file,
      # We add compiler_classpath to ensure the scala-library jar is on the classpath.
      # TODO: This also adds the compiler jar to the classpath, which compiled code shouldn't
      # usually need. Be more selective?
      '-classpath', ':'.join(self._compiler_classpath + classpath),
      '-d', output_dir
    ])
    args.extend(sources)
    self.log_zinc_file(analysis_file)
    return self.run_zinc(args, workunit_labels=[WorkUnit.COMPILER])

  # Run zinc in analysis manipulation mode.
  def run_zinc_analysis(self, analysis_file, args, workunit_name='analysis'):
    zinc_analysis_args = [
      '-analysis',
      '-cache', analysis_file,
    ]
    zinc_analysis_args.extend(args)
    return self.run_zinc(args=zinc_analysis_args, workunit_name=workunit_name, workunit_labels=[WorkUnit.COMPILER])

  # src_cache - split this analysis cache.
  # splits - a list of (sources, dst_cache), where sources is a list of the sources whose analysis
  #          should be split into dst_cache.
  def run_zinc_split(self, src_analysis_file, splits):
    # Must use the abspath of the sources, because that's what Zinc uses internally.
    zinc_split_args = [
      '-split', ','.join(
        ['{%s}:%s' % (':'.join([os.path.abspath(p) for p in x[0]]), x[1]) for x in splits]),
    ]
    self.log_zinc_file(src_analysis_file)
    return self.run_zinc_analysis(src_analysis_file, zinc_split_args, workunit_name='split')

  # src_analysis_files - a list of analysis files to merge into dst_analysis_file.
  def run_zinc_merge(self, src_analysis_files, dst_analysis_file):
    zinc_merge_args = [
      '-merge', ':'.join(src_analysis_files),
    ]
    for analysis_file in src_analysis_files:
      self.log_zinc_file(analysis_file)
    return self.run_zinc_analysis(dst_analysis_file, zinc_merge_args, workunit_name='merge')

  # cache - the analysis cache to rebase.
  # rebasings - a list of pairs (rebase_from, rebase_to). Behavior is undefined if any
  # rebase_from is a prefix of any other, as there is no guarantee that rebasings are
  # applied in a particular order.
  def run_zinc_rebase(self, analysis_file, rebasings):
    zinc_rebase_args = [
      '-rebase', ','.join(['%s:%s' % rebasing for rebasing in rebasings]),
    ]
    return self.run_zinc_analysis(analysis_file, zinc_rebase_args, workunit_name='rebase')

  IVY_HOME_PLACEHOLDER = '/IVY_HOME_PLACEHOLDER'
  PANTS_HOME_PLACEHOLDER = '/PANTS_HOME_PLACEHOLDER'

  def relativize_analysis_file(self, src, dst):
    # Make an analysis cache portable. Work on a tmpfile, for safety.
    #
    # NOTE: We can't port references to deps on the Java home. This is because different JVM
    # implementations on different systems have different structures, and there's not
    # necessarily a 1-1 mapping between Java jars on different systems. Instead we simply
    # drop those references from the analysis cache.
    #
    # In practice the JVM changes rarely, and it should be fine to require a full rebuild
    # in those rare cases.
    with temporary_dir() as tmp_analysis_dir:
      tmp_analysis_file = os.path.join(tmp_analysis_dir, "analysis")
      ZincUtils._copy_analysis(src, tmp_analysis_file)
      rebasings = [
        (self._java_home, ''),  # Erase java deps.
        (self._ivy_home, ZincUtils.IVY_HOME_PLACEHOLDER),
        (self._pants_home, ZincUtils.PANTS_HOME_PLACEHOLDER),
      ]
      exit_code = self.run_zinc_rebase(tmp_analysis_file, rebasings)
      if not exit_code:
        ZincUtils._copy_analysis(tmp_analysis_file, dst)
      return exit_code

  def localize_analysis_file(self, src, dst):
    with temporary_dir() as tmp_analysis_dir:
      tmp_analysis_file = os.path.join(tmp_analysis_dir, "analysis")
      ZincUtils._copy_analysis(src, tmp_analysis_file)
      rebasings = [
        (ZincUtils.IVY_HOME_PLACEHOLDER, self._ivy_home),
        (ZincUtils.PANTS_HOME_PLACEHOLDER, self._pants_home),
      ]
      exit_code = self.run_zinc_rebase(tmp_analysis_file, rebasings)
      if not exit_code:
        ZincUtils._copy_analysis(tmp_analysis_file, dst)
      return exit_code

  def write_plugin_info(self, resources_dir, target):
    basedir = os.path.join(resources_dir, target.id)
    with safe_open(os.path.join(basedir, _PLUGIN_INFO_FILE), 'w') as f:
      f.write(textwrap.dedent('''
        <plugin>
          <name>%s</name>
          <classname>%s</classname>
        </plugin>
      ''' % (target.plugin, target.classname)).strip())
    return basedir, _PLUGIN_INFO_FILE

  # These are the names of the various jars zinc needs. They are, conveniently and
  # non-coincidentally, the names of the flags used to pass the jar locations to zinc.
  compiler_jar_names = ['scala-library', 'scala-compiler', 'scala-reflect']  # Compiler version.
  zinc_jar_names = ['compiler-interface', 'sbt-interface' ]  # Other jars zinc needs pointers to.

  @staticmethod
  def identify_zinc_jars(zinc_classpath):
    """Find the named jars in the zinc classpath.

    TODO: When profiles migrate to regular pants jar() deps instead of ivy.xml files we can
          make these mappings explicit instead of deriving them by jar name heuristics.
    """
    ret = OrderedDict()
    ret.update(ZincUtils.identify_jars(ZincUtils.zinc_jar_names, zinc_classpath))
    return ret

  @staticmethod
  def identify_jars(names, jars):
    jars_by_name = {}
    jars_and_filenames = [(x, os.path.basename(x)) for x in jars]

    for name in names:
      jar_for_name = None
      for jar, filename in jars_and_filenames:
        if filename.startswith(name):
          jar_for_name = jar
          break
      if jar_for_name is None:
        raise TaskError('Couldn\'t find jar named %s' % name)
      else:
        jars_by_name[name] = jar_for_name
    return jars_by_name

  def find_plugins(self, plugin_names):
    """Returns a map from plugin name to plugin jar."""
    plugin_names = set(plugin_names)
    plugins = {}
    # plugin_jars is the universe of all possible plugins and their transitive deps.
    # Here we select the ones to actually use.
    for jar in self._plugin_jars:
      with open_jar(jar, 'r') as jarfile:
        try:
          with closing(jarfile.open(_PLUGIN_INFO_FILE, 'r')) as plugin_info_file:
            plugin_info = ElementTree.parse(plugin_info_file).getroot()
          if plugin_info.tag != 'plugin':
            raise TaskError(
              'File %s in %s is not a valid scalac plugin descriptor' % (_PLUGIN_INFO_FILE, jar))
          name = plugin_info.find('name').text
          if name in plugin_names:
            if name in plugins:
              raise TaskError('Plugin %s defined in %s and in %s' % (name, plugins[name], jar))
            # It's important to use relative paths, as the compiler flags get embedded in the zinc
            # analysis file, and we port those between systems via the artifact cache.
            plugins[name] = os.path.relpath(jar, self._pants_home)
        except KeyError:
          pass

    unresolved_plugins = plugin_names - set(plugins.keys())
    if len(unresolved_plugins) > 0:
      raise TaskError('Could not find requested plugins: %s' % list(unresolved_plugins))
    return plugins

  def log_zinc_file(self, analysis_file):
    self.context.log.debug('Calling zinc on: %s (%s)' % (analysis_file, hash_file(analysis_file).upper() if os.path.exists(analysis_file) else 'nonexistent'))

  @staticmethod
  def is_nonempty_analysis(path):
    """Returns true iff path exists and points to a non-empty analysis."""
    relfile = path + '.relations'
    if not os.path.exists(relfile):
      return False
    empty_prefix = 'products:\n\n'  # Empty analyses have a blank line instead of products.
    with open(relfile, 'r') as infile:
      prefix = infile.read(len(empty_prefix))
    return prefix != empty_prefix

  @staticmethod
  def _move_analysis(src, dst):
    ZincUtils.copy_or_move_analysis(shutil.move, src, dst)

  @staticmethod
  def _copy_analysis(src, dst):
    ZincUtils.copy_or_move_analysis(shutil.copy, src, dst)

  @staticmethod
  def copy_or_move_analysis(func, src, dst):
    if os.path.exists(src):
      func(src, dst)
      relations = src + '.relations'
      if os.path.exists(relations):
        func(relations, dst + '.relations')
