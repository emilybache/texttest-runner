
import os, sys, re, hashlib, time
from xml.dom.minidom import parse
from ordereddict import OrderedDict
from glob import glob
from pprint import pprint

class AbortedException(RuntimeError):
    pass

class JobStillRunningException(RuntimeError):
    pass

class FingerprintNotReadyException(RuntimeError):
    pass
            
def getBuildsDir(jobRoot, jobName):
    projectDir = os.path.join(jobRoot, jobName)
    local = os.path.join(projectDir, "builds")
    if os.path.isdir(local):
        return local
        
    if hasattr(os, "readlink"):
        link = os.path.join(projectDir, "lastStable")
        try:
            target = os.readlink(link)
            return os.path.dirname(target)
        except OSError:
            return

class BuildDocument:
    versionRegex = re.compile("[0-9]+(\\.[0-9]+)+")
    versionRegexRpm = re.compile("[0-9]+(\\.[0-9]+)+.*.rpm")       
    @classmethod
    def create(cls, buildsDir, buildName):
        xmlFile = os.path.join(buildsDir, buildName, "build.xml")
        if os.path.isfile(xmlFile):
            return cls(xmlFile)
        
    def __init__(self, xmlFile):
        self.document = parse(xmlFile)

    def fingerprintStrings(self):
        for obj in self.document.getElementsByTagName("hudson.tasks.Fingerprinter_-FingerprintAction"):
            for entry in obj.getElementsByTagName("string"):
                yield entry.childNodes[0].nodeValue
                
    def getResult(self):
        for entry in self.document.getElementsByTagName("result"):
            return entry.childNodes[0].nodeValue
    
    def checkHashes(self, oldHashes, newHashes):
        for currString in self.fingerprintStrings():
            if currString in oldHashes:
                return True, False
            elif currString in newHashes:
                return True, True
        return False, False

    def getArtefactVersion(self, artefactRegex):
        for currString in self.fingerprintStrings():
            if artefactRegex.match(currString):
                versionMatch = self.versionRegex.search(currString)
                if versionMatch:
                    return versionMatch.group(0)

    def getFingerprint(self, ignoreArtefact):
        prevString = None
        fingerprint = {}
        for currString in self.fingerprintStrings():
            if prevString:
                if ignoreArtefact not in prevString:
                    vregex = self.versionRegexRpm if prevString.endswith(".rpm") else self.versionRegex
                    match = vregex.search(prevString)
                    regex = prevString
                    if match:
                        regex = prevString.replace(match.group(0), vregex.pattern) + "$"
                    fingerprint[regex] = currString, prevString
                prevString = None
            else:
                prevString = currString
        return fingerprint

            
class FingerprintVerifier:
    def __init__(self, fileFinder, cacheDir):
        self.fileFinder = fileFinder
        self.cacheDir = cacheDir
        
    def getCacheFileName(self, buildName):
        return os.path.join(self.cacheDir, "correct_hashes_" + buildName)

    def getCachedFingerprint(self, buildName):
        cacheFileName = self.getCacheFileName(buildName) 
        if os.path.isfile(cacheFileName):
            return eval(open(cacheFileName).read())

    def writeCache(self, buildName, updatedHashes):
        cacheFileName = self.getCacheFileName(buildName)
        with open(cacheFileName, "w") as f:
            pprint(updatedHashes, f) 
        
    def md5sum(self, filename):
        md5 = hashlib.md5()
        with open(filename,'rb') as f: 
            for chunk in iter(lambda: f.read(128*md5.block_size), b''): 
                md5.update(chunk)
        return md5.hexdigest()
    
    def getCorrectedHash(self, buildsDir, build, f, hash):
        fullFileFinder = os.path.join(buildsDir, build, self.fileFinder)
        filePattern = f.split(":")[-1].replace("-", "?")
        paths = glob(os.path.join(fullFileFinder, filePattern))
        if len(paths):
            path = paths[0]
            correctHash = self.md5sum(path)
            if correctHash != hash:
                return correctHash
            

class FingerprintDifferenceFinder:
    def __init__(self, jobRoot, fileFinder, cacheDir):
        self.jobRoot = jobRoot
        self.verifier = FingerprintVerifier(fileFinder, cacheDir) if fileFinder else None
                
    def findDifferences(self, jobName, build1, build2):
        buildsDir = getBuildsDir(self.jobRoot, jobName)
        if not buildsDir:
            return []
        fingerprint1 = self.getFingerprint(buildsDir, jobName, build1)
        fingerprint2 = self.getAndWaitForFingerprint(buildsDir, jobName, build2)
        if not fingerprint1 or not fingerprint2:
            return []
        differences = []
        updatedHashes = {}
        for artefact, (hash2, file2) in fingerprint2.items():
            hash1 = fingerprint1.get(artefact)
            if isinstance(hash1, tuple):
                hash1 = hash1[0]
            if hash1 != hash2:
                if self.verifier:
                    correctedHash = self.verifier.getCorrectedHash(buildsDir, build2, file2, hash2)
                    if correctedHash:
                        hash2 = correctedHash
                        updatedHashes[artefact] = hash2
                    if hash1 == hash2:
                        continue
                differences.append((artefact, hash1, hash2))
        
        if updatedHashes:
            print "WARNING: incorrect hashes found!"
            print "This is probably due to fingerprint data being wrongly updated from artefacts produced during the build"
            print "Storing a cached file of corrected versions. The following were changed:"
            for artefact, hash in updatedHashes.items():
                print artefact, fingerprint2.get(artefact)[0], hash
                
            for artefact, (hash2, file2) in fingerprint2.items():
                if artefact not in updatedHashes:
                    updatedHashes[artefact] = hash2
            self.verifier.writeCache(build2, updatedHashes)
        
        differences.sort()
        return differences

    def getAndWaitForFingerprint(self, *args):
        for i in range(500):
            try:
                return self.getFingerprint(*args)
            except FingerprintNotReadyException:
                if i % 10 == 0:
                    print "No Jenkins fingerprints available yet, sleeping..."
                time.sleep(1)
                    
        print "Giving up waiting for fingerprints."
        raise JobStillRunningException()
    
    def getFingerprint(self, buildsDir, jobName, buildName):
        if self.verifier:
            cached = self.verifier.getCachedFingerprint(buildName)
            if cached:
                return cached
    
        document = BuildDocument.create(buildsDir, buildName)
        fingerprint = {}
        if document is None:
            return fingerprint
        
        fingerprint = document.getFingerprint(jobName)
        if not fingerprint:
            result = document.getResult()
            if result is None and os.getenv("BUILD_NUMBER") == buildName and os.getenv("JOB_NAME") == jobName:
                if os.getenv("BUILD_ID") == "none": 
                    # Needed to prevent Jenkins from killing background jobs running after the job has exited
                    # If we have this, we should wait a bit
                    raise FingerprintNotReadyException()
                else:
                    raise JobStillRunningException()
            # No result means aborted (hard) if we're checking a previous run, otherwise it means we haven't finished yet
            elif result == "ABORTED" or result is None:
                raise AbortedException, "Aborted in Jenkins"
        return fingerprint


class ChangeSetFinder:
    def __init__(self, jobRoot, jenkinsUrl, bugSystemData):
        self.jobRoot = jobRoot
        self.jenkinsUrl = jenkinsUrl
        self.bugSystemData = bugSystemData
        
    def getChangeSetData(self, projectChanges):
        changes = []
        for project, build in projectChanges:
            buildsDir = getBuildsDir(self.jobRoot, project)
            if buildsDir is None:
                continue
            xmlFile = os.path.join(buildsDir, build, "changelog.xml")
            if os.path.isfile(xmlFile):
                document = parse(xmlFile)
                authors = []
                bugs = []
                for changeset in document.getElementsByTagName("changeset"):
                    author = self.parseAuthor(changeset.getAttribute("author"))
                    if author not in authors:
                        authors.append(author)
                    for msgNode in changeset.getElementsByTagName("msg"):
                        msg = msgNode.childNodes[0].nodeValue
                        self.addUnique(bugs, self.getBugs(msg))
                if authors:
                    fullUrl = os.path.join(self.jenkinsUrl, "job", project, build, "changes")
                    changes.append((",".join(authors), fullUrl, bugs))
        return changes
    
    def parseAuthor(self, author):
        withoutEmail = author.split("<")[0].strip().split("@")[0]
        if "." in withoutEmail:
            return " ".join([ part.capitalize() for part in withoutEmail.split(".") ])
        else:
            return withoutEmail.encode("ascii", "xmlcharrefreplace")
        
    def addUnique(self, items, newItems):
        for newItem in newItems:
            if newItem not in items:
                items.append(newItem)
        
    def getBugs(self, msg):
        bugs = []
        for systemName, location in self.bugSystemData.items():
            try:
                exec "from default.knownbugs." + systemName + " import getBugsFromText"
                self.addUnique(bugs, getBugsFromText(msg, location)) #@UndefinedVariable
            except ImportError:
                pass
        return bugs

    
class ProjectData:
    def __init__(self, jobRoot):
        self.data = {}
        workspaceRoot = os.path.dirname(os.getenv("WORKSPACE"))
        for jobName in os.listdir(workspaceRoot):
            jobDir = os.path.join(jobRoot, jobName)
            if os.path.isdir(jobDir):
                subdir = self.getSubdirectory(jobDir)
                workspaceDir = os.path.join(workspaceRoot, jobName, subdir)
                for artefactName, providedScope in self.getArtefactsFromPomFiles(workspaceDir):
                    self.data.setdefault(artefactName, []).append((jobName, providedScope))
                    
    def isAttachedRpm(self, pluginNode):
        return any((goalNode.childNodes[0].nodeValue == "rpm-maven-plugin" for goalNode in pluginNode.getElementsByTagName("artifactId")))
    
    def getRpmName(self, node):
        for pluginNode in node.getElementsByTagName("plugin"):
            if self.isAttachedRpm(pluginNode):
                for confNode in pluginNode.childNodes:
                    if confNode.nodeName == "configuration":
                        for nameNode in confNode.childNodes:
                            if nameNode.nodeName == "name":
                                return nameNode.childNodes[0].nodeValue
    
    def getPomData(self, pomFile):
        document = parse(pomFile)
        artifactId, groupId = None, None
        rpmName = None
        artefacts, modules = [], []
        for node in document.documentElement.childNodes:
            if artifactId is None and node.nodeName == "artifactId":
                artifactId = node.childNodes[0].nodeValue
            elif groupId is None and node.nodeName == "groupId":
                groupId = node.childNodes[0].nodeValue
            elif node.nodeName == "modules":
                for subNode in node.childNodes:
                    if subNode.childNodes:
                        modules.append(subNode.childNodes[0].nodeValue)
            elif node.nodeName == "build":
                rpmName = self.getRpmName(node)
        providedScope = any((node.childNodes[0].nodeValue == "provided" for node in document.getElementsByTagName("scope")))
        groupPrefix = groupId + ":" if groupId else ""
        artefacts.append((groupPrefix + artifactId, providedScope))
        if rpmName:
            artefacts.append((groupPrefix + rpmName, True))
        return artefacts, modules
    
    def getArtefactsFromPomFiles(self, workspaceDir):
        pomFile = os.path.join(workspaceDir, "pom.xml")
        if not os.path.isfile(pomFile):
            return []
            
        artefacts, modules = self.getPomData(pomFile)
        for module in modules:
            moduleDir = os.path.join(workspaceDir, module)
            artefacts += self.getArtefactsFromPomFiles(moduleDir)
        return artefacts
    
    def getSubdirectory(self, jobDir):
        configFile = os.path.join(jobDir, "config.xml")
        if not os.path.isfile(configFile):
            return ""
        
        document = parse(configFile)
        for subDir in document.getElementsByTagName("subdir"):
            return subDir.childNodes[0].nodeValue
        return ""
    
    def getProjects(self, artefact):
        currProjArtefact = None
        currProjects = []
        for projArtefact, projects in self.data.items():
            if currProjArtefact is None or len(projArtefact) > len(currProjArtefact):
                if artefact.startswith(projArtefact):
                    currProjArtefact = artefact
                    currProjects = projects
                elif ":" in projArtefact:
                    group, local = projArtefact.split(":", 1)
                    if artefact.startswith(local):
                        currProjArtefact = group + ":" + artefact
                        currProjects = projects
        
        return currProjArtefact or artefact, currProjects
    

class ChangeFinder:
    def __init__(self, bugSystemData, markedArtefacts, *args):
        self.jobRoot = os.path.join(os.getenv("JENKINS_HOME"), "jobs")
        self.jobName = os.getenv("JOB_NAME")
        self.projectData = ProjectData(self.jobRoot)
        self.markedArtefacts = markedArtefacts
        self.changeSetFinder = ChangeSetFinder(self.jobRoot, os.getenv("JENKINS_URL"), bugSystemData)
        self.diffFinder = FingerprintDifferenceFinder(self.jobRoot, *args)
    
    def findChanges(self, build1, build2):
        try:
            markedChanges, projectChanges = self.getChangesRecursively(self.jobName, build1, build2)
        except AbortedException, e:
            # If it was aborted, say this
            return [(str(e), "", [])]
        
        # Extract the changeset information from them
        changesFromProjects = self.changeSetFinder.getChangeSetData(projectChanges)
        changesFromMarking = [ self.getMarkChangeText(artefact, projectName, build1, build2) for artefact, projectName in markedChanges ]
        return changesFromMarking + changesFromProjects
    
    def getChangesRecursively(self, jobName, build1, build2):
        # Find what artefacts have changed between times build
        differences = self.diffFinder.findDifferences(jobName, build1, build2)
        # Organise them by project
        markedChanges, differencesByProject = self.organiseByProject(differences)
        # For each project, find out which builds were affected
        projectChanges, recursiveChanges = self.getProjectChanges(differencesByProject)
        for subProj, subBuild1, subBuild2 in recursiveChanges:
            if subProj != jobName:
                subMarkedChanges, subProjectChanges = self.getChangesRecursively(subProj, subBuild1, subBuild2)
                for subMarkChange in subMarkedChanges:
                    if subMarkChange not in markedChanges:
                        markedChanges.append(subMarkChange)
                for subProjectChange in subProjectChanges:
                    if subProjectChange not in projectChanges:
                        projectChanges.append(subProjectChange)
        return markedChanges, projectChanges
    
    def organiseByProject(self, differences):
        differencesByProject = OrderedDict()
        changes = []
        for artefact, oldHash, hash in differences:
            actualArtefact, projects = self.projectData.getProjects(artefact)
            if projects:
                for project, scopeProvided in projects:
                    differencesByProject.setdefault(project, []).append((actualArtefact, oldHash, hash, scopeProvided))
                    if project in self.markedArtefacts:
                        changes.append((actualArtefact, project))
            else:
                projectName = artefact.split(":")[-1].split("[")[0][:-1]
                if projectName in self.markedArtefacts:
                    changes.append((actualArtefact, projectName))
        
        return changes, differencesByProject
    
    def getProjectChanges(self, differencesByProject):
        projectChanges = []
        recursiveChanges = []
        for project, diffs in differencesByProject.items():
            buildsDir = getBuildsDir(self.jobRoot, project)
            if buildsDir is None:
                continue
            allBuilds = sorted([ build for build in os.listdir(buildsDir) if build.isdigit()], key=lambda b: -int(b))
            oldHashes = [ oldHash for _, oldHash, _, _ in diffs ]
            newHashes = [ hash for _, _, hash, _ in diffs ]
            scopeProvided = any((s for _, _, _, s in diffs))  
            activeBuild = None
            for build in allBuilds:
                document = BuildDocument.create(buildsDir, build)
                if not document:
                    continue
        
                if document.getResult() != "FAILURE":
                    matched, matchedNew = document.checkHashes(oldHashes, newHashes)
                    if matched:
                        if matchedNew:
                            activeBuild = build
                        else:
                            if scopeProvided and activeBuild:
                                recursiveChanges.append((project, build, activeBuild))
                            break       
                if activeBuild and (project, build) not in projectChanges:
                    projectChanges.append((project, build))
        return projectChanges, recursiveChanges
    
    def getMarkChangeText(self, artefact, projectName, build1, build2):
        buildsDir = getBuildsDir(self.jobRoot, self.jobName)
        regex = re.compile(artefact)
        version1 = BuildDocument.create(buildsDir, build1).getArtefactVersion(regex)
        version2 = BuildDocument.create(buildsDir, build2).getArtefactVersion(regex)
        if version1 == version2:
            return projectName + " was updated", "", []
        else:
            return projectName + " " + version2, "", []
    

def getChanges(build1, build2, *args):
    finder = ChangeFinder(*args)
    return finder.findChanges(build1, build2)
    
def getTimestamp(build):
    if hasattr(os, "readlink"):
        jobRoot = os.path.join(os.getenv("JENKINS_HOME"), "jobs")
        buildsDir = getBuildsDir(jobRoot, os.getenv("JOB_NAME"))
        if buildsDir:
            buildLink = os.path.join(buildsDir, build)
            if os.path.exists(buildLink):
                return os.readlink(buildLink)
    
def parseEnvAsList(varName):
    if varName in os.environ:
        return os.getenv(varName).split(",")
    else:
        return []
        
def parseEnvAsDict(varName):
    ret = {}
    for pairText in parseEnvAsList(varName):
        var, value = pairText.split("=")
        ret[var] = value
    return ret
    
if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    buildName = sys.argv[1]
    if len(sys.argv) > 2:
        prevBuildName = sys.argv[2]
    else:
        prevBuildName = str(int(buildName) - 1)
    pprint(getChanges(prevBuildName, buildName, parseEnvAsDict("BUG_SYSTEM_DATA"), parseEnvAsList("MARKED_ARTEFACTS"), 
                      os.getenv("FILE_FINDER", ""), os.getcwd()))
    