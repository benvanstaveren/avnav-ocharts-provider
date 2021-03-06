import datetime
import json
import os
import signal
import re
import sys
import time
import traceback
import urllib2
import subprocess
import platform
import shutil
import threading


class Plugin:
  EXENAME="AvnavOchartsProvider"
  SERVERNAME="oeserverd"
  STARTSCRIPT="provider.sh"
  ENV_NAME="AVNAV_PROVIDER"
  CONFIG_FILE="avnav.conf"
  CONFIG=[
        {
          'name':'enabled',
          'description':'set to true to enable plugin',
          'default':'true'
        },
        {
          'name':'port',
          'description':'the listener port for the chart provider executable',
          'default':'8082'
        },
        {
          'name': 'internalPlugin',
          'description': 'use the plugin installed below our own root dir',
          'default':'true'
        },
        {
          'name':'configdir',
          'description':'directory for cfg files',
          'default': '$DATADIR/ocharts'
        },
        {
          'name':'ocpnPluginDir',
          'description':'directory for OpenCPN plugins',
          'default':''
        },
        {
          'name': 'exeDir',
          'description': 'directory oeserverd',
          'default': ''
        },
        {
          'name':'s57DataDir',
          'description': 'parent directory for s57data',
          'default':''
        },
        {
          'name':'threads',
          'description':'number of provider threads',
          'default':"5"
        },
        {
          'name':'debug',
          'description':'debuglevel for provider',
          'default':'1'
        },
        {
          'name':'chartdir',
          'description':'temp location for charts',
          'default': ''
        },
        {
          'name':'scale',
          'description':'scale for provider',
          'default':"2"
        },
        {
          'name':'cacheSize',
          'description':'number of tiles in cache',
          'default': '10000'
        },
        {
          'name': 'diskCacheSize',
          'description': 'number of tiles in cache on disk per set',
          'default': '400000'
        },
        {
          'name': 'prefillZoom',
          'description': 'max zoom level for cache prefill',
          'default': '17'
        },
        {
          'name': 'memPercent',
          'description':'percent of existing mem to be used',
          'default':''

        }
      ]

  @classmethod
  def pluginInfo(cls):
    """
    the description for the module
    @return: a dict with the content described below
            parts:
               * description (mandatory)
               * data: list of keys to be stored (optional)
                 * path - the key - see AVNApi.addData, all pathes starting with "gps." will be sent to the GUI
                 * description
    """
    return {
      'description': 'ocharts provider for AvNav',
      'version': '1.0',
      'config':cls.CONFIG,
      'data': [

      ]
    }

  def __init__(self,api):
    """
        initialize a plugins
        do any checks here and throw an exception on error
        do not yet start any threads!
        @param api: the api to communicate with avnav
        @type  api: AVNApi
    """
    self.api = api
    self.config={}
    self.baseUrl=None #will be set in run
    self.connected=False
    self.chartList=[]


  def checkProviderProcess(self,exe):
    """
    return a list with pid,ppid,uid for running chartproviders
    :param path:
    :param onlyOwnChild:
    :return:
    """
    process = subprocess.Popen(['ps', '-o' 'pid,uid', '--no-headers', '-C', exe],stdout=subprocess.PIPE,stderr=None,stdin=None,close_fds=True)
    list=[]
    while True:
      if process.stdout.closed:
        break
      line=process.stdout.readline()
      if line is None or line == '':
        break
      line=line.rstrip().lstrip()
      pvs=re.split("  *",line)
      if len(pvs) < 2:
        continue
      try:
        list.append((int(pvs[0]),int(pvs[1])))
      except:
        self.api.debug("strange line in ps output %s"%line)
    process.wait()
    return list

  def isPidRunning(self,pid):
    ev=self.getEnvValueFromPid(pid)
    if ev is None:
      return False
    return ev == self.getEnvValue()

  def getEnvValueFromPid(self,pid):
    envValue = None
    try:
      BUFSIZE = 1024
      evFile = "/proc/%d/environ" % pid
      lastEnv = ''
      with open(evFile, "rb") as f:
        while envValue is None:
          ev = f.read(BUFSIZE)
          if (ev is None  or ev == '') and lastEnv == '':
            break
          if ev is None:
            ev=lastEnv+chr(0)+chr(0)
          else:
            ev=lastEnv+ev
          lastEnv=''
          if ev.find(chr(0)) >= 0:
            evlist = ev.split(chr(0))
            evlist[0] += lastEnv
            lastEnv = evlist[-1]
            for entry in evlist[0:-1]:
              nv = entry.split("=")
              if len(nv) < 2:
                continue
              if nv[0] == self.ENV_NAME:
                envValue = nv[1]
                break
          else:
            lastEnv += ev
        f.close()
    except Exception as e:
      self.api.debug("unable to read env for pid %d: %s" % (pid, e))
    return envValue
  
  def filterProcessList(self,list,checkForEnv=False):
    """
    filter a list returned by checkProviderProcess for own user
    :param list:
    :param checkForParent: also filter out processes with other parent
    :return:
    """
    rt=[]
    for entry in list:
      if entry[1] != os.getuid():
        continue
      if checkForEnv:
        envValue=self.getEnvValueFromPid(entry[0])
        if envValue is None or envValue != self.getEnvValue():
          continue
      rt.append(entry)
    return rt

  #we only allow one provider per config dir
  def getEnvValue(self):
    configdir = self.config['configdir']
    return platform.node()+":"+configdir

  def getCmdLine(self):
    exe=os.path.join(os.path.dirname(__file__),self.STARTSCRIPT)
    if not os.path.exists(exe):
      raise Exception("executable %s not found"%exe)
    ocpndir=self.config['ocpnPluginDir']
    if not os.path.isdir(ocpndir):
      raise Exception("OpenCPN plugin directory %s not found"%ocpndir)
    s57dir=self.config['s57DataDir']
    if not os.path.isdir(s57dir) or not os.path.isdir(os.path.join(s57dir,"s57data")):
      pdir=os.path.dirname(__file__)
      fallbackBase=os.path.join(pdir,"share","opencpn")
      fallbackS57Dir=os.path.join(fallbackBase,"s57data")
      if os.path.isdir(fallbackS57Dir):
        self.api.log("configured s57data dir %s not found, using internal fallback %s"%(s57dir,fallbackBase))
        s57dir=fallbackBase
      else:
        raise Exception("S57 data directory(parent) %s not found (and no fallback dir)"%s57dir)
    configdir = self.config['configdir']
    if not os.path.exists(configdir):
      raise Exception("config dir %s not found" % configdir)
    logname = os.path.join(configdir, "provider.log")
    chartdir=self.config['chartdir']
    chartdirs=re.split(" *, *",chartdir.rstrip().lstrip())
    for chart in chartdirs:
      if chart == '':
        continue
      if not os.path.isdir(chart):
        raise Exception("chart dir %s not found"%chart)
    cmdline = ["/bin/sh",exe, '-t',self.config['threads'],'-d',self.config['debug'], '-s',self.config['scale'], '-l' , logname, '-p', str(os.getpid()),
               "-c",self.config['cacheSize'],
               "-f",self.config['diskCacheSize'],
               "-r",self.config['prefillZoom'],
               "-e", self.config['exeDir']]
    if self.config['memPercent'] != '':
      cmdline= cmdline + ["-x",self.config['memPercent']]
    cmdline=cmdline + [ocpndir,
               s57dir, configdir, str(self.config['port'])]+chartdirs
    return cmdline

  def handleProcessOutput(self,process):
    buffer=process.stdout.readline()
    while buffer is not None and buffer != "":
      self.api.log("PROVIDEROUT: %s",buffer)
      buffer=process.stdout.readline()

  def startProvider(self):
    cmdline=self.getCmdLine()
    envValue = self.getEnvValue()
    env = os.environ.copy()
    PATH= env.get('PATH')
    if PATH is None:
      PATH=self.config['exeDir']
    else:
      PATH=PATH+os.path.pathsep+self.config['exeDir']
    env.update({self.ENV_NAME: envValue,'PATH':PATH})
    self.api.log("starting provider with command %s"%" ".join(cmdline))
    process=subprocess.Popen(cmdline,env=env,close_fds=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
    if process is None:
      raise Exception("unable to start provider with %s"," ".join(cmdline))
    reader=threading.Thread(target=self.handleProcessOutput,args=[process])
    reader.start()
    return process

  def listCharts(self,hostip):
    self.api.debug("listCharts %s"%hostip)
    if not self.connected:
      self.api.debug("not yet connected")
      return []
    try:
      items=self.chartList+[]
      for item in items:
        for k in item.keys():
          if type(item[k]) == str or type(item[k]) == unicode:
            item[k]=item[k].replace("localhost",hostip).replace("127.0.0.1",hostip)
      return items
    except:
      self.api.debug("unable to contact provider: %s"%traceback.format_exc())
      return []
  MANDATORY_DIRS={
    'ocpnPluginDir':os.path.join("lib","opencpn"),
    'exeDir':'bin',
    's57DataDir':os.path.join("share","opencpn")
  }
  def run(self):
    """
    the run method
    this will be called after successfully instantiating an instance
    this method will be called in a separate Thread
    The example simply counts the number of NMEA records that are flowing through avnav
    and writes them to the store every 10 records
    @return:
    """
    enabled = self.api.getConfigValue('enabled','true')
    if enabled.lower() != 'true':
      self.api.setStatus("INACTIVE","module not enabled in server config")
      self.api.error("module disabled")
      return

    for cfg in self.CONFIG:
      v=self.api.getConfigValue(cfg['name'],cfg['default'])
      if v is None:
        self.api.error("missing config value %s"%cfg['name'])
        self.api.setStatus("INACTIVE", "missing config value %s"%cfg['name'])
        return
      self.config[cfg['name']]=v

    for name in self.config.keys():
      if type(self.config[name]) == str or type(self.config[name]) == unicode:
        self.config[name]=self.config[name].replace("$DATADIR",self.api.getDataDir())
        self.config[name] = self.config[name].replace("$PLUGINDIR", os.path.dirname(__file__))
    useInternalPlugin=self.config['internalPlugin']
    if useInternalPlugin == '':
      useInternalPlugin='true'
    rootBase="/usr"
    baseDir=rootBase
    if useInternalPlugin.lower() == 'true':
      baseDir=os.path.dirname(__file__)
      if not os.path.exists(os.path.join(baseDir,"lib","opencpn")) and os.path.exists(os.path.join(rootBase,"lib","opencpn")):
        self.api.error("internal plugin is set but path does not exist, using external")
        baseDir=rootBase
    for mdir in self.MANDATORY_DIRS.keys():
      if self.config[mdir]  == '':
        self.config[mdir] = os.path.join(baseDir,self.MANDATORY_DIRS[mdir])
      dir=self.config[mdir]
      if not os.path.isdir(dir):
        self.api.error("mandatory directory %s (path: %s) not found"%(mdir,dir))
        self.api.setStatus("ERROR","mandatory directory %s (path: %s) not found"%(mdir,dir))
        return
    configdir=self.config['configdir']
    if not os.path.isdir(configdir):
      self.api.log("configdir %s does not (yet) exist"%configdir)
      os.makedirs(configdir)
    if not os.path.isdir(configdir):
      self.api.error("unable to create config dir %s"%configdir)
      self.api.setStatus("ERROR","unable to create config dir %s"%configdir)
      return
    cfgfile=os.path.join(configdir,self.CONFIG_FILE)
    if not os.path.exists(cfgfile):
      try:
        src=os.path.join(os.path.dirname(__file__),self.CONFIG_FILE)
        if os.path.exists(src):
          self.api.log("config file %s does not exist, creating initial from %s"%(cfgfile,src))
          shutil.copyfile(src,cfgfile)
        else:
          self.api.log("config file %s does not exist, creating empty",src)
          with open(cfgfile,"") as f:
            f.write("")
            f.close()
      except Exception as e:
        self.api.error("unable to create config file %s",cfgfile)
        self.api.setStatus("ERROR","unable to create config file %s"%cfgfile)
        return
    port=None
    try:
      port=int(self.config['port'])
    except:
      self.api.error("exception while reading port from config %s",traceback.format_exc())
      self.api.setStatus("ERROR","invalid value for port %s"%self.config['port'])
      return
    processes=self.checkProviderProcess(self.EXENAME)
    own=self.filterProcessList(processes,True)
    alreadyRunning=False
    providerPid=-1
    if len(processes) > 0:
      if len(own) != len(processes):
        self.api.log("there are provider processes running from other users: %s",",".join(map(lambda x: str(x[0]),list(set(processes)-set(own)))))
      if len(own) > 0:
        #TODO: handle more then one process
        self.api.log("we already see a provider running with pid %d, trying this one"%filtered[0][0])
        alreadyRunning=True
        providerPid=own[0][0]
    if not alreadyRunning:
      self.api.log("starting provider process")
      self.api.setStatus("STARTING","starting provider process %s"%self.STARTSCRIPT)
      try:
        process=self.startProvider()
        providerPid=process.pid
        time.sleep(5)
      except Exception as e:
        self.api.error("unable to start provider: %s",traceback.format_exc())
        self.api.setStatus("ERROR","unable to start provider %s"%e)
        return
    self.api.log("started with port %d"%port)
    self.baseUrl="http://localhost:%d/list"%port
    self.api.registerChartProvider(self.listCharts)
    self.api.registerUserApp("http://$HOST:%d/static/index.html"%port,"gui/icon.png")
    reported=False
    errorReported=False
    self.api.setStatus("STARTED", "provider started with pid %d, connecting at %s" %(providerPid,self.baseUrl))
    ready=False
    while True:
      responseData=None
      try:
        response=urllib2.urlopen(self.baseUrl,timeout=10)
        if response is None:
          raise Exception("no response on %s"%self.baseUrl)
        responseData=json.loads(response.read())
        if responseData is None:
          raise Exception("no response on %s"%self.baseUrl)
        status=responseData.get('status')
        if status is None or status != 'OK':
          raise Exception("invalid status from provider query")
        self.chartList=responseData['items']
      except:
        self.api.debug("exception reading from provider %s"%traceback.format_exc())
        self.connected=False
        filteredList=self.filterProcessList(self.checkProviderProcess(self.EXENAME),True)
        if len(filteredList) < 1:
          if self.isPidRunning(providerPid):
            self.api.debug("final executable not found, but started process is running, wait")
          else:
            self.api.setStatus("STARTED", "restarting provider")
            self.api.log("no running provider found, trying to start")
            #just see if we need to kill some old child...
            backgroundList=self.filterProcessList(self.checkProviderProcess(self.SERVERNAME),True)
            for bp in backgroundList:
              pid=bp[0]
              self.api.log("killing background process %d",pid)
              os.kill(pid,signal.SIGKILL)
            try:
              process=self.startProvider()
              providerPid=process.pid
              self.api.setStatus("STARTED", "provider restarted with pid %d, trying to connect at %s"%(providerPid,self.baseUrl))
            except Exception as e:
              self.api.error("unable to start provider: %s"%traceback.format_exc())
              self.api.setStatus("ERROR", "unable to start provider %s"%e)
        else:
          providerPid=filteredList[0][0]
          self.api.setStatus("STARTED","provider started with pid %d, trying to connect at %s" % (providerPid, self.baseUrl))
        if reported:
          if not errorReported:
            self.api.error("lost connection at %s"%self.baseUrl)
            errorReported=True
          reported=False
          self.api.setStatus("ERROR","lost connection at %s"%self.baseUrl)
        time.sleep(1)
        continue
      errorReported=False
      self.connected=True
      if not reported:
        self.api.log("got first provider response")
        self.api.setStatus("NMEA","provider (%d) sucessfully connected at %s"%(providerPid,self.baseUrl))
        reported=True
      time.sleep(1)








