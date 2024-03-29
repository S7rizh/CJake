import os
import json
import re
import tempfile
import subprocess
import logging
import datetime
import sys
import getopt
import collections
import xml.etree.ElementTree as ET
from pathlib import Path
from collections import deque
from pprint import pprint

TARGETS_JSON_FILE = "target_files.json"
FORMATS = (".cpp", ".c", ".h", ".hpp")
# FORMATS = (".h", ".hpp")

PROCESS_FILES = False
PROCESS_DIRS = True

# Formatting varaibles

PRINT_ALL = False
USAGE_VIEW = False

# Processing options

ONLY_C_STYLE = False
PROCESS_ALTERNATIVES = True

# Logging

LOG_TO_STDOUT = True
LOG_NAME_FORMAT = "CJake log %H-%M-%S %d-%m-%Y.log"
LOG_LEVEL = logging.DEBUG

# Arguments parsing

PARSE_ARGUMENTS = True

### Imported code
### from http://code.activestate.com/recipes/576694/
class OrderedSet(collections.MutableSet):

    def __init__(self, iterable=None):
        self.end = end = [] 
        end += [None, end, end]         # sentinel node for doubly linked list
        self.map = {}                   # key --> [key, prev, next]
        if iterable is not None:
            self |= iterable

    def __len__(self):
        return len(self.map)

    def __contains__(self, key):
        return key in self.map

    def add(self, key):
        if key not in self.map:
            end = self.end
            curr = end[1]
            curr[2] = end[1] = self.map[key] = [key, curr, end]

    def discard(self, key):
        if key in self.map:        
            key, prev, next = self.map.pop(key)
            prev[2] = next
            next[1] = prev

    def __iter__(self):
        end = self.end
        curr = end[2]
        while curr is not end:
            yield curr[0]
            curr = curr[2]

    def __reversed__(self):
        end = self.end
        curr = end[1]
        while curr is not end:
            yield curr[0]
            curr = curr[1]

    def pop(self, last=True):
        if not self:
            raise KeyError('set is empty')
        key = self.end[1][0] if last else self.end[2][0]
        self.discard(key)
        return key

    def __repr__(self):
        if not self:
            return '%s()' % (self.__class__.__name__,)
        return '%s(%r)' % (self.__class__.__name__, list(self))

    def __eq__(self, other):
        if isinstance(other, OrderedSet):
            return len(self) == len(other) and list(self) == list(other)
        return set(self) == set(other)

### End of imported code

class DependencyNode:
    def __init__(self, file_path, name, parent, preprocessing_includes):
        self.file_path = file_path
        self.name = name
        self.dependencies = []
        self.parents = []
        if parent:
            self.add_parent(parent)
        # self.implementation = None
        self.structure = None
        self.required_functions = {}    # name -> [{name, start_line, end_line}, ..]
                                        # Dictionary is needed to keep uniqueness of function entities
        self.root = False
        self.header = None

        # Extract structure
        self.extract_functions(preprocessing_includes)

    def set_as_root(self):
        self.root = True
    
    def _find_node(self, search_list, target):
        for node in search_list:
            if node.name == target.name:
                return True
        return False

    def add_parent(self, parent):   # TODO : possible performance bottleneck
        # Check if this parent already exists
        if not self._find_node(self.parents, parent):
            self.parents.append(parent)
            parent.add_dependency(self)
    
    def add_dependency(self, dep):
        if not self._find_node(self.dependencies, dep):
            self.dependencies.append(dep)
            dep.add_parent(self)
    
    def extract_functions(self, includes):
        if not self.file_path:
            return
        # Creating temporary directory to work with
        with tempfile.TemporaryDirectory() as tempdir:
            # tempdir = "./temp" # debug
            extension = os.path.splitext(self.file_path)
            prep_file_path = os.path.join(tempdir, "prep{}".format(extension[1]))

            # prep_file_path = os.path.join(tempdir, os.path.basename(self.file_path))
            # Creating temprorary file containing source code
            with open(prep_file_path, "w+") as prep_file:
                # Preprocessing source code
                gcc_command = ["gcc"]
                for path in includes:
                    gcc_command.append("-I" + path)
                gcc_command.append("-E")
                gcc_command.append("-P")
                gcc_command.append(self.file_path)
                gcc_process = subprocess.Popen(gcc_command, stdout=prep_file)
                gcc_process.wait()

                # Run doxygen

                doxy_command = ["doxygen"]
                doxy_command.append(os.path.join(os.getcwd(), "Doxyfile"))
                doxy_process = subprocess.Popen(doxy_command, cwd=tempdir, stdout=subprocess.DEVNULL)
                doxy_process.wait()

                # Compiling results in one XML file using XSLT

                # xsltproc -o output.xml combine.xslt index.xml
                # xslt_output_file_path = os.path.join(tempdir, "xml", "xslt_output.xml")
                # with open(xslt_output_file_path, "w+") as xslt_output_file:
                xslt_command = ["xsltproc", "-o", "xslt_output.xml", "combine.xslt", "index.xml"]
                xslt_process = subprocess.Popen(xslt_command, cwd=os.path.join(tempdir, "xml"), stdout=subprocess.DEVNULL)
                xslt_process.wait()

                # Extract information from XML

                file_structure = {
                    "class":[],
                    "function":[],  # TODO : Need more information to store about functions (bodystart, bodyend)
                    "variable":[],
                    "typedef":[]
                }

                # doxy_xml_path = os.path.join(tempdir, "xml", "{}_8{}.{}".format("prep", extension[1][1:], "xml"))
                doxy_xml_path = os.path.join(tempdir, "xml", "xslt_output.xml")
                with open(doxy_xml_path) as doxy_xml:
                    tree = ET.parse(doxy_xml)
                    root = tree.getroot()
                    for compound in root:
                        # Add new name of class if it is not known
                        if not compound.get("kind") == "file" and not compound.find("compoundname").text == "std":
                            if not compound.get("kind") in file_structure.keys():
                                logging.debug("New compound kind '{}'".format(compound.get("kind")))
                                file_structure[compound.get("kind")] = []
                            # file_structure[compound.get("kind")].append(compound.find("compoundname").text)
                            file_structure[compound.get("kind")].append({
                                "name" : compound.find("compoundname").text,
                                "start_line" : None,
                                "end_line" : None,
                            })

                        for section in compound:
                            if section.tag == "innerclass":
                                # file_structure["class"].append(section.text)
                                file_structure["class"].append({
                                    "name" : section.text,
                                    "start_line" : None,
                                    "end_line" : None,
                                })
                            elif section.tag == "sectiondef":
                                for member in section:
                                    # Convert line numbers to int of not None 
                                    start_line = member.find("location").get("bodystart")
                                    if start_line:
                                        start_line = int(start_line)

                                    end_line = member.find("location").get("bodyend")
                                    if end_line:
                                        end_line = int(end_line)

                                    struct = {
                                        "name" : member.find("name").text,
                                        "start_line" : start_line,
                                        "end_line" : end_line,
                                    }
                                    if not member.get("kind") in file_structure.keys():
                                        logging.warning("New type {} appeared in the file structure".format(member.find("name").text))
                                        file_structure[member.get("kind")] = [struct]
                                    else:
                                        # file_structure[member.get("kind")].append(member.find("name").text)
                                        file_structure[member.get("kind")].append(struct)
                            # print("<{}> {} {}".format(section.tag, section.get("kind"), section.find("name")))
                        # print(file_structure)

                    self.structure = file_structure

            # os.system("gcc {} > {}/prep.{}")

    def _compare_functions(self, f1, f2):
        if f1['name'] == f2['name'] and \
           f1["start_line"] == f2["start_line"] and \
           f1["end_line"] == f2["end_line"]:
            return True
        else:
            return False

    def _find_file_coverage(self, target_lines):
        # The intersection of the structures takes place
        # Due to this reason, need to combine target lines so that
        # there will be no intersections

        sorted_target_lines = sorted(target_lines, key=lambda x : x[0])
        new_target_lines = []

        current_start = 0
        current_end = 0

        for start_line, end_line in sorted_target_lines:
            if start_line > current_end:
                new_target_lines.append((current_start, current_end))
                current_start = start_line
                if end_line == -1:
                    current_end = current_start
                else:
                    current_end = end_line
            else:
                current_end = max(current_end, end_line)
        
        new_target_lines.append((current_start, current_end))

        logging.debug("Combined target lines : " + str(new_target_lines))

        return new_target_lines[1:]

    def find_used_functions(self):
            
        # TODO : ADD GLOBAL VARIABLES TOO
        # Go through dependencies and make dictionary of them
        keywords_table = {}

        # Process all structures in dependencies
        for dep in self.dependencies:
            # for name in dep.values():
            if not dep:
                logging.warning("None is passed as dependency")
                continue
            if not dep.file_path:
                logging.warning("file '{}' not found to find usages".format(dep.name))
                continue
            # logging.debug("dep '{}'".format(dep.name))
            # logging.debug("path='{}'".format(dep.file_path))
            # logging.debug("structure[function] = {}".format(dep.structure["function"]))

            # Combining all constructions from a file to one list
            struct_keywords = []    # Contains all constructions in dependencies
            if ONLY_C_STYLE:    # Need only functions and variables
                struct_keywords = dep.structure["function"] + dep.structure["variable"]
            else:   # Include everything
                for key in dep.structure.keys():
                    # if not (key == "function" or key == "variable"):
                    #     continue
                    struct_keywords.extend(dep.structure[key])

            # Processing constructions 
            for func in struct_keywords:
                if func["name"] in keywords_table.keys():
                    if PROCESS_ALTERNATIVES:
                        keywords_table[func["name"]].append((dep, func))
                    else:
                        logging.warning("duplicating keys '{}' at '{}'. New dependency '{}'".format(\
                            func["name"], self.name, dep.name))
                else:
                    keywords_table[func["name"]] = [(dep, func)]

        # Process all structures in the current file
        struct_local = []   # Contains all constructions in this file
        file_functions = {}

        if ONLY_C_STYLE:    # Need only functions and variables
            struct_local = self.structure["function"] + self.structure["variable"]
        else:   # Include everything
            for key in self.structure.keys():
                struct_local.extend(self.structure[key])
            
        for func in struct_local:
            if func["name"] in file_functions.keys():
                if PROCESS_ALTERNATIVES:
                    file_functions[func["name"]].append((self, func))
            else:
                file_functions[func["name"]] = [(self, func)]
        
        logging.debug("Processing functions at '{}', path='{}', required functions : {}".format(self.name, self.file_path, str(self.required_functions.keys())))
        # logging.debug("is subset : {}".format(str(OrderedSet(file_functions.keys()).issubset(keywords_table.keys()))))

        # Find subset of included keywords
        appeared_keywords = set()   # TODO : FIX, ORDERED SET IS GIVING STABLE RESULTS

        pattern = "\\b" + "\\b|\\b".join(keywords_table.keys()) + "\\b"  # regex pattern to find keywords from dependencies
        logging.debug("Pattern applied '{}'".format(pattern))
        if not pattern:
            logging.warning("No keywords for '{}', path '{}'".format(self.name, self.file_path))
            return []

        local_pattern = "\\b" + "\\b|\\b".join(file_functions.keys()) + "\\b" # Pattern to find local file functions


        if self.root:   # If it is a root node, go through the whole file
            with open(self.file_path) as f:
                for str_idx, content in enumerate(f):
                    [appeared_keywords.add(key) for key in re.findall(pattern, content)]
        else:
            # Create list of needed lines

            new_target_lines = []   # For the functions declared in this file

            for func_list in self.required_functions.values():
                for func in func_list:
                    # functions having no body_start or body_end assumed to be prototypes
                    if not func["start_line"] or not func["end_line"]:
                        continue

                    new_target_lines.append((func["start_line"], func["end_line"]))
            

            used_local_functions = set(self.required_functions.keys())  # TODO Can be ordered set, but not sure

            # re.findall if needed line is reached
            with open(self.file_path) as f:
                while new_target_lines: # While we have something new to add
                    # target_lines = sorted(new_target_lines, key=lambda x : x[0])
                    target_lines = self._find_file_coverage(new_target_lines)
                    current_range_idx = 0

                    new_target_lines.clear()

                    for str_idx, content in enumerate(f):
                        if current_range_idx == len(target_lines):
                            break   # No more ranges left
                        current_range = target_lines[current_range_idx]
                        if isinstance(current_range[0], str):
                            logging.debug("str found instead of int '{}'".format(current_range[0]))
                        if isinstance(current_range[1], str):
                            logging.debug("str found instead of int '{}'".format(current_range[1]))

                        # Append result of re.findall if it is body of needed element
                        if (current_range[0] - 1 <= str_idx and str_idx <= current_range[1] - 1) \
                            or (current_range[1] == -1 and current_range[0] - 1 == str_idx):
                            # Add found keywords
                            # [appeared_keywords.add(key) for key in re.findall(pattern, content)]
                            for key in re.findall(pattern, content):
                                appeared_keywords.add(key)
                            # Add new functions ranges for the next iteration
                            for local_func_name in re.findall(local_pattern, content):
                                if local_func_name in used_local_functions:
                                    continue
                                used_local_functions.add(local_func_name)
                                # new_target_lines.append(file_functions[local_func_name][1])
                                for self_dep, local_func in file_functions[local_func_name]:
                                    # local_func = file_functions[local_func_name][1]
                                    if local_func_name in self.required_functions.keys():
                                        is_in_required = False
                                        for existing_func in self.required_functions[local_func_name]:
                                            if self._compare_functions(existing_func, local_func):
                                                is_in_required = True
                                                break
                                        if not is_in_required:
                                            self.required_functions[local_func_name].append(local_func)
                                        else:
                                            continue
                                    else:
                                        self.required_functions[local_func_name] = [local_func]

                                    if not local_func["start_line"] or not local_func["end_line"]:
                                        continue
                                    new_target_lines.append((local_func["start_line"], local_func["end_line"]))

                        # if str_idx >= current_range[1] - 1:   # Current range is ended
                        #     prev_range = current_range
                        #     current_range_idx += 1
                        #     # Also check if there are repeating ranges
                        #     while current_range_idx != len(target_lines) and \
                        #           target_lines[current_range_idx][0] == prev_range[0] and \
                        #           target_lines[current_range_idx][1] == prev_range[1]:
                        #         current_range_idx += 1

                        # while   current_range_idx != len(target_lines) and \
                        #         str_idx > target_lines[current_range_idx][1] - 1:
                        #     current_range_idx += 1
                        if str_idx == target_lines[current_range_idx][1] - 1:
                            # If at the of current range, increase counter
                            current_range_idx += 1

        # Add required functions to corresponding nodes
        logging.debug("keys found in '{}'".format(self.file_path))
        updated_nodes = []
        for key in appeared_keywords:
            if not key in keywords_table.keys():
                logging.warning("Unknown key was found ({})".format(key))
                continue
            for keyword_node, keyword_function in keywords_table[key]:
                # if key in keyword_node.structure["function"]:
                # keyword_node.required_functions.add(keyword_function)

                # TODO : Need to check functions if there was a recursive call or external.

                if not key in keyword_node.required_functions.keys():
                    logging.debug("found key: '{}' from '{}'".format(key, keyword_node.name))
                    keyword_node.required_functions[key] = [keyword_function]
                else:
                    # Check if this function is already there
                    is_in_required = False
                    for existing_func in keyword_node.required_functions[key]:
                        # if existing_func["start_line"] ==  keyword_function["start_line"] and \
                        #    existing_func["end_line"] ==  keyword_function["end_line"]:
                        if self._compare_functions(existing_func, keyword_function):
                            is_in_required = True
                            break

                    if not is_in_required:
                        keyword_node.required_functions[key].append(keyword_function)
                    else:
                        continue    # Don't add to updated_nodes
                        
                if not self._find_node(updated_nodes, keyword_node):
                    updated_nodes.append(keyword_node)
        
        return updated_nodes


class Analyzer:
        
    def _extract_files_from_dirs(self, dirs):
        # search_files = {}
        search_files = []
        filenames = []
        duplicating = {}
        for dir_path in dirs:
            for root, directories, files in  os.walk(dir_path):
                for f in files:
                    if not any(ext in f for ext in FORMATS):
                        continue
                    file_path = os.path.join(root,f)
                    # if file_path in files:
                    if f in filenames:
                        logging.warning("Duplicating files are found '{}'".format(f))
                        # duplicating.append(f)
                        if f in duplicating.keys():
                            duplicating[f].append(file_path)
                        else:
                            duplicating[f] = [file_path]
                    else:
                        # search_files[f] = file_path
                        search_files.append(file_path)
                    # search_filenames.append(f)
                    # search_filepaths.append(file_path)

        # Print duplicates
        for name, file_paths in duplicating.items():
            print("({}) -> {}".format(name, file_paths))
        
        return search_files

    def __init__(self, json_file):
        self.targets = None
        self.known_dependencies = []
        self.edge_dependencies = []
        self.root_nodes = []
        self.processing_stack = []
        with open(TARGETS_JSON_FILE) as json_file:
            self.targets = json.load(json_file)
        self.starting_files = []

        # Find files to start with
        if PROCESS_FILES:
            self.starting_files = self.targets["Files"]
        if PROCESS_DIRS:
            new_files = self._extract_files_from_dirs(self.targets['Dirs'])
            if self.starting_files:
                self.starting_files.extend(new_files)
            else:
                self.starting_files = new_files
        self.starting_files = list(set(self.starting_files))

        # Extracting files to search
        self.search_files = self._extract_files_from_dirs(self.targets['Search_dirs'])
        self.search_files.extend(self.starting_files)

        # Extracting files to search edge files
        self.edge_dirs = self._extract_files_from_dirs(self.targets['Edge_search_dirs'])

        # Preprocessing includes
        self.preprocessing_includes = self.targets["Preprocessing_includes"]

    def is_known_node(self, dep):
        for d in self.known_dependencies:
            if d.file_path == dep.file_path:
                return True
        return False
    
    def is_known_dep_name(self, d_name):
        for d in self.known_dependencies:
            if d.name == d_name:
                return d
        return None
    
    def is_edge_dep_name(self, d_name):
        for d in self.edge_dependencies:
            if d.name == d_name:
                return d
        return None

    def find_file(self, dependecy_name):
        # print("DEP : {}".format(dependecy_name))
        for path in self.search_files:
            if path.endswith(dependecy_name):
                # print(path)
                return path
        return None

    def find_edge_filepath(self, edge_dep_name):
        for path in self.edge_dirs:
            if path.endswith(edge_dep_name):
                return path
        logging.warning("Edge dependency '{}' filepath not found ".format(edge_dep_name))
        return None

    def find_includes(self, dep_node):
        dependency_list = []
        with open(dep_node.file_path) as f: # Dependencies of implementation if it exists
            for str_idx, content in enumerate(f):
                # Seems that this pattern finds only platform independent includes (probably some programming convention
                # is used by OpenJDK developers)

                # new_include = re.findall(r"#include (\".*\"|<.*>)", content)  

                new_include = re.findall(r"#include (\".*\"|<.*>)", content)
                if new_include:
                    # Cutting brackets
                    if len(new_include) > 1:
                        logging.warning("More than one matches of include per string")
                    dependency_list.append(new_include[0][1:-1])

        # Dependencies found in the header
        if dep_node.header:

            with open(dep_node.header) as f: # Dependencies of implementation if it exists
                for str_idx, content in enumerate(f):
                    new_include = re.findall(r"#include (\".*\"|<.*>)", content)
                    if new_include:
                        # Cutting brackets
                        if len(new_include) > 1:
                            logging.warning("More than one matches of include per string")
                        if not new_include[0][1:-1] in dependency_list:
                            dependency_list.append(new_include[0][1:-1])
                        else:
                            logging.warning("Header and implementation have duplicating includes '{}'".format(dep_node.name))

        return dependency_list

    def find_header_implementation(self, filename):
        # Assuming that implementation is in the same folder
        if not (filename.endswith(".h") or filename.endswith(".hpp")):
            # print("WARNING : Not a header")
            return None
        
        c_file_path = None
        cpp_file_path = None

        if filename.endswith(".h"):
            c_file_path = filename[:-2] + ".c"
            cpp_file_path = filename[:-2] + ".cpp"
        else:
            c_file_path = filename[:-4] + ".c"
            cpp_file_path = filename[:-4] + ".cpp"


        # c_file_path = os.path.join(root, filename[:-2] + ".c")
        # cpp_file_path = os.path.join(root, filename[:-2] + ".cpp")

        c_file_config = Path(c_file_path)
        cpp_file_config = Path(cpp_file_path)

        if c_file_config.is_file() and cpp_file_config.is_file():
            logging.warning(".c and .cpp implementations")

        if c_file_config.is_file():
            return c_file_path

        if cpp_file_config.is_file():
            return cpp_file_path

        logging.warning("implementation is not found [{}]".format(filename))
        return None


    def print_edge_deps(self):
        # filtered_deps = sorted(self.edge_dependencies, key=lambda x: x.name)
        filtered_deps = sorted(self.edge_dependencies, key=lambda x: len(x.parents))
        if USAGE_VIEW: # Print usage of dependencies by searched files
            files = {}
            for d in filtered_deps:
                for f in d.parents:
                    if f.name in files.keys():
                        files[f.name].append(d.name)
                    else:
                        files[f.name] = [d.name]
            filtered_files = sorted(files.items(), key=lambda x: len(x[1]))
            for item in filtered_files:
                print("{} uses {} : {}".format(item[0], str(len(item[1])), str(item[1])))
        else:
            for d in filtered_deps:
                if len(d.parents) <= 3 or PRINT_ALL:
                    print("'{}' used by {} : {}".format(d.name, len(d.parents), [p.name for p in d.parents]))
                else:
                    print("'{}' used by {}".format(d.name, len(d.parents)))
        print("Overall edge files: {}".format(len(self.edge_dependencies)))
    
    def print_edge_functions_report(self):
        print("#################### Functions report ####################")
        used_modules_count = 0
        entities_count = 0
        sorted_edge_dependencies = sorted(self.edge_dependencies, key=lambda x : x.name)
        for dep in sorted_edge_dependencies:
            print("Module '{}', filepath '{}'".format(dep.name, dep.file_path))
            if dep.required_functions.keys():
                used_modules_count += 1
            for f_name in sorted(dep.required_functions.keys()):
                print("    {},".format(f_name))
                entities_count += 1
        print("\n{}/{} modules used, {} entities required".format(used_modules_count, len(self.edge_dependencies), entities_count))

    def print_debug_structures(self):
        processing_queue = deque()
        for node in self.root_nodes:
            processing_queue.append(node)
        
        processed_names = OrderedSet(self.root_nodes)

        while processing_queue:
            current_node = processing_queue.popleft()
            print("----------------------------------------")
            print("Node name={}, path={}".format(current_node.name, current_node.file_path))
            print("\n")
            pprint(current_node.structure)
            print("\nREQUIRED FUNCTIONS")
            print(current_node.required_functions)
            # print("----------------------------------------")
            for dep in current_node.dependencies:
                if dep.name in processed_names:
                    continue
                processing_queue.append(dep)
                processed_names.add(dep.name)

    def resolve(self):
        # Loading starting files
        for f in self.starting_files:
            root_node = DependencyNode(f, os.path.basename(f), None, self.preprocessing_includes)
            root_node.set_as_root()
            self.root_nodes.append(root_node)
            self.processing_stack.append(root_node)

        # Building trees. There are 3 states of files: 
        # new -> not processed yet, 
        # known -> found in search directories, 
        # edge -> not found in search diorectories, leaf node.
        while self.processing_stack:
            current_file = self.processing_stack.pop()
            # if current_file in self.known_dependencies:
            # if self.is_known_node(current_file):
            #     continue
            # self.known_dependencies.append(current_file)
            deps = self.find_includes(current_file)
            for d_name in deps:
                # Process new nodes
                d_node = self.is_known_dep_name(d_name) # If known and already know, add parent
                if d_node:
                    d_node.add_parent(current_file)
                else:
                    d_node = self.is_edge_dep_name(d_name) #If edge and already have, add parent
                    if d_node:
                        d_node.add_parent(current_file)
                    else:   # Else try to find in search files and add to needed list
                        d_path = self.find_file(d_name)
                        if d_path:
                            # Find implementation and use its path to extract needed information
                            i_path = self.find_header_implementation(d_path)
                            if i_path:
                                d_node = DependencyNode(i_path, d_name, current_file, self.preprocessing_includes)
                                d_node.header = d_path
                            else:
                                d_node = DependencyNode(d_path, d_name, current_file, self.preprocessing_includes)
                            self.processing_stack.append(d_node)
                            self.known_dependencies.append(d_node)
                        else:
                            d_node = DependencyNode(d_path, d_name, current_file, self.preprocessing_includes)
                            self.edge_dependencies.append(d_node)

        # Processing edge files
        not_found_files = OrderedSet()

        for e_node in self.edge_dependencies:
            e_node.file_path = self.find_edge_filepath(e_node.name)
            if not e_node.file_path:
                not_found_files.add(e_node.name)
            e_node.extract_functions(self.preprocessing_includes)

        # Analyzing dependent functions

        # Queue to use
        code_processing_queue = deque()
        names_in_queue = set()    # Paths that are already in the queue

        # Process root nodes first
        for node in self.root_nodes:
            code_processing_queue.append(node)
            # names_in_queue = set()
            names_in_queue.add(node.name)

        while code_processing_queue:
            current_node = code_processing_queue.popleft()
            names_in_queue.remove(current_node.name)
            if current_node.name in not_found_files:
                continue
            
            logging.debug("Code processing queue - current node : '{}'".format(current_node.name))
            updated_deps = current_node.find_used_functions()
            # for dep in current_node.dependencies:
            for dep in updated_deps:
                if not dep.name in names_in_queue:
                    code_processing_queue.append(dep)
                    names_in_queue.add(dep.name)
        
        # Output needed results
        self.print_edge_deps()

        self.print_edge_functions_report()

        # self.print_debug_structures()

        
            
            

        
        

def parse_args():
    usage_str = """python analysis_tool.py -h -a -c -l -f target_files.json
    -h for help
    -a to process alternatives
    -c to process only C functions and variables
    -l output logs to the file
    -f process files set in 'Files' in target_files.json
    target_files.json is a path to file containing settings (./target_files.json if not set)
    The output is passed to STDOUT"""

    opts = None
    args = None

    try:
        opts, args = getopt.getopt(sys.argv[1:], "aclf")
    except getopt.GetoptError:
        print("Wrong arguments. Usage {}".format(usage_str))
        sys.exit(2)
    
    for opt, arg in opts:
        print(opt)
        if opt == '-h':
            print(usage_str)
            sys.exit(0)
        elif opt == '-a':
            global PROCESS_ALTERNATIVES
            PROCESS_ALTERNATIVES = True
            print("PROCESS_ALTERNATIVES = {}".format(str(PROCESS_ALTERNATIVES)))
        elif opt == '-c':
            global ONLY_C_STYLE
            ONLY_C_STYLE = True
        elif opt == '-l':
            global LOG_TO_STDOUT
            LOG_TO_STDOUT = False
        elif opt == '-f':
            global PROCESS_FILES
            PROCESS_FILES = True
    
    if args:
        TARGETS_JSON_FILE = args[0]
    

if __name__ == "__main__":

    if PARSE_ARGUMENTS:
        parse_args()

    if not LOG_TO_STDOUT:
        logging.basicConfig(filename=datetime.datetime.today().strftime(LOG_NAME_FORMAT), \
                            level=LOG_LEVEL)

    tool = Analyzer(TARGETS_JSON_FILE)
    tool.resolve()

    # Debug code

    # includes = [
    #     "macros_headers/jdk8/hotspot/src/share/vm",
    #     "macros_headers/jdk8/hotspot/src/share/vm/prims",
    #     "macros_headers/jdk8/hotspot/src/share/vm/precompiled",
    #     "macros_headers/jdk8/hotspot/src/cpu/x86/vm/prims",
    #     "macros_headers/jdk8/hotspot/src/cpu/x86/vm",
    #     "macros_headers/generated",
    #     "macros_headers/c_cpp_standard/7",
    #     "macros_headers/c_cpp_standard/backward",
    #     "macros_headers/c_cpp_standard/include",
    #     "macros_headers/c_cpp_standard/include-fixed",
    # ]
    # dn = DependencyNode("../jdk8/hotspot/src/share/vm/prims/jvm.cpp", "jvm.cpp", None)
    # dn.extract_functions(includes)
    # for dep in dn.dependencies:
    #     dep.extract_functions()
    # dn.set_as_root()
    # dn.find_used_functions()
    # for dep in dn.dependencies:
    #     print("{} : {}".format(dep.name, dep.required_functions))

    