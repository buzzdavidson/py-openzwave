#!/usr/bin/env python
# -*- coding: utf-8 -*-
from collections import namedtuple

import curses
import curses.panel
import logging
import threading
import time
from louie import dispatcher, All
from ozwWrapper import ZWaveWrapper

padcoords = namedtuple('padcoords', ['sminrow','smincol','smaxrow','smaxcol'])

class ZWaveCommander:
    def __init__(self, stdscr):
        self._curAlert = False
        self._alertStack = list()
        self._driverInitialized = False
        self._wrapper = None
        self._listMode = True
        self._screen = stdscr
        self._version = '0.1 Beta 1'
        self._listtop = 0
        self._listindex = 0
        self._listcount = 0
        self._stop = threading.Event()
        self._keys = {
            'A' : 'Add',
            'B' : 'About',
            'D' : 'Delete',
            'R' : 'Refresh',
            'S' : 'Setup',
            '+' : 'Increase',
            '-' : 'Decrease',
            '1' : 'On',
            '0' : 'Off',
            'Q' : 'Quit'
        }

        self._config = {
            'device': '/dev/keyspan-2',
            'config': 'openzwave/config/',
        }

        # TODO: add log level to config
        # TODO: add log enable/disable to config
        # TODO: logging - can ozw log be redirected to file?  If so, we can add ability to view/tail log
        FORMAT='%(asctime)s\t%(levelname)s\t%(name)s\t%(message)s'
        logging.basicConfig(filename='test.log', level=logging.DEBUG, format=FORMAT)
        self._log = logging.getLogger('ZWaveCommander')
        self._logbar ='\n%s\n' % ('-'*60)

    def main(self):
        '''Main run loop'''
        self._log.info('%sZWaveCommander Version %s Starting%s', self._logbar, self._version, self._logbar)
        self._initCurses(self._screen)
        try:
            self._checkConfig()
            self._checkInterface()
            self._runLoop()
        finally:
            self._shutdown()

    def _delayloop(self, context, duration, callback):
        self._log.debug('thread %s sleeping...', context)
        time.sleep(duration)
        self._log.debug('timer %s expired, executing callback %s', context, callback)
        if context == 'alert':
            self._curAlert = False
            if self._alertStack:
                self._alert(self._alertStack.pop())
        if callback is not None:
            callback()

    def _handleQuit(self):
        # TODO: exit confirmation dialog
        self._log.info('Stop requested')
        self._stop.set()
        
    def _setTimer(self, context, duration, callback):
        newTimer = threading.Thread(None, self._delayloop, 'cb-thread-%s' % context, (context, duration, callback), {})
        newTimer.setDaemon(True)
        newTimer.start()

    def _alert(self, text):
        '''perform program alert'''
        if not self._curAlert:
            self._curAlert = True
            curses.flash()
            self._screen.addstr(self._screensize[0] - 1, 0, ' {0:{width}}'.format(text, width=self._screensize[1] - 2),
                            curses.color_pair(self.COLOR_ERROR))
            self._screen.refresh()
            self._setTimer('alert', 1, self._redrawMenu)
        else:
            self._alertStack.append(text)

    def _layoutScreen(self):
        # TODO: handle screen resize on curses.KEY_RESIZE in loop (tear down, re-calculate, and re-build)
        # top 5 lines (fixed): system info (including list header)
        # bottom line (fixed): menu/status
        # remaining top half: item list (scrolling)
        # remaining bottom half: split - left half=static info, right half=detail (scrolling)
        # item list: 8 columns. All column widths here are padded with 1 char space (except col 0, which is always 1 char)
        # c0=1 char fixed (select indicator)
        # c1=4 char fixed (id)
        # c2=10 char min (name)
        # c3=10 char min (location)
        # c4=20 char min (type)
        # c5=9 char fixed (state)
        # c6=7 char fixed (batt)
        # c7=7 char fixed (signal)
        # last three columns: 23 chars: are optional and can fall off if space requires it (min width 45)
        # "min" columns expand evenly to fit remaining space

        self._screen.clear()
        self._log.debug("Laying out screen")
        self._colwidths=[1,4,10,10,20,9,7,7]
        self._colheaders=['','ID','Name','Location','Type','State','Batt','Signal']
        self._detailheaders=['Info','Config','Values','Classes','Groups']
        self._flexcols=[2,3,4]
        self._rowheights=[5,5,10,1]
        self._flexrows=[1,2]

        self._sortcolumn = self._colheaders[1]
        self._detailview = self._detailheaders[0]

        self._screensize = self._screen.getmaxyx()
        width = self._screensize[1]
        height = self._screensize[0]
        self._log.debug('Screen is %d wide by %d high', width, height)

        # Update dynamic column widths for device list
        self._log.debug('Initial column widths are: %s', self._colwidths)
        cwid = 0
        for i in self._colwidths: cwid += i
        flexwidth = width - cwid
        if flexwidth > 0:
            adder = divmod(flexwidth, len(self._flexcols))
            for i in self._flexcols:
                self._colwidths[i] += adder[0]
            self._colwidths[self._flexcols[-1]] += adder[1]
        self._log.debug('Adjusted column widths are: %s' ,self._colwidths)

        # Update dynamic row heights for screen sections
        self._log.debug('Initial row heights are: %s' , self._rowheights)
        cht = 0
        for i in self._rowheights: cht += i
        flexheight = height - cht
        if flexheight > 0:
            adder = divmod(flexheight, len(self._flexrows))
            for i in self._flexrows:
                self._rowheights[i] += adder[0]
            self._rowheights[self._flexrows[-1]] += adder[1]
        self._log.debug('Adjusted row heights are: %s' , self._rowheights)

        self._listpad = curses.newpad(256,256)
        self._updateColumnHeaders()

    def _initCurses(self, stdscr):
        '''Configure ncurses application-specific environment (ncurses has already been initialized)'''
        curses.curs_set(0)

        # Re-define color attributes...
        self.COLOR_NORMAL=1
        self.COLOR_HEADER_NORMAL=2
        self.COLOR_HEADER_HI=3
        self.COLOR_ERROR=4
        self.COLOR_CRITICAL=5
        self.COLOR_WARN=6
        self.COLOR_OK=7

        curses.init_pair(self.COLOR_NORMAL, curses.COLOR_WHITE, curses.COLOR_BLACK) # normal (selected row is inverted, disabled/sleep is dim)
        curses.init_pair(self.COLOR_HEADER_NORMAL, curses.COLOR_BLACK, curses.COLOR_GREEN) # header normal
        curses.init_pair(self.COLOR_HEADER_HI, curses.COLOR_WHITE, curses.COLOR_CYAN) # header hi
        curses.init_pair(self.COLOR_ERROR, curses.COLOR_YELLOW, curses.COLOR_RED) # error text
        curses.init_pair(self.COLOR_CRITICAL, curses.COLOR_RED, curses.COLOR_BLACK) # critical
        curses.init_pair(self.COLOR_WARN, curses.COLOR_YELLOW, curses.COLOR_BLACK) # warn
        curses.init_pair(self.COLOR_OK, curses.COLOR_GREEN, curses.COLOR_BLACK) # ok

        self._layoutScreen()

    def _checkConfig(self):
        # TODO: check if configuration exists and is valid.  If not, then go directly to handleSetup().  Loop until user cancels or enters valid config.
        pass

    def _handleSetup(self):
        self._alert('handleSetup not yet implemented')

    def _checkIfInitialized(self):
        if not self._driverInitialized:
            msg = 'Unable to initialize driver - check configuration'
            self._alert(msg)
            self._log.warning(msg)
            self._handleSetup()
        else:
            self._log.info('OpenZWave initialized successfully.')

    def _notifyDriverReady(self, homeId):
        self._log.info('OpenZWave Driver is Ready; homeid is %0.8x.  %d nodes were found.', homeId, self._wrapper.nodeCount)
        self._driverInitialized = True
        self._addDialogText(2,'Driver initialized with homeid {0}'.format(hex(homeId)))
        self._addDialogText(3,'Node Count is now {0}'.format(self._wrapper.nodeCount))
        self._readyNodeCount = 0

    def _notifyNodeAdded(self, homeId, nodeId):
        self._addDialogText(3,'Node Count is now {0}'.format(self._wrapper.nodeCount))
        self._updateSystemInfo()

    def _redrawAll(self):
        self._clearDialog()
        self._updateSystemInfo()
        self._updateDeviceList()
        self._updateColumnHeaders()
        self._updateDeviceDetail()

    def _notifySystemReady(self):
        self._log.info('OpenZWave Initialization Complete.')
        self._alert('OpenZWave Initialization Complete.')
        self._redrawAll()

    def _notifyNodeReady(self, homeId, nodeId):
        self._readyNodeCount += 1
        self._addDialogText(2, 'OpenZWave is querying associated devices')
        self._addDialogText(3,'Node {0} is now ready'.format(nodeId))
        self._addDialogProgress(5, self._readyNodeCount, self._wrapper.nodeCount)
        self._updateDeviceList()

    def _notifyValueChanged(self, signal, **kw):
        pass
        # TODO: handle value changed notification

    def _initDialog(self, height, width, buttons=('OK',), caption=None):
        self._dialogpad = curses.newpad(height, width)
        self._dialogpad.bkgd(0x94, curses.color_pair(self.COLOR_HEADER_HI))
        self._dialogpad.clear()
        self._dialogpad.box()
        if caption:
           lh = (width / 2) - (len(caption) / 2) - 1
           self._dialogpad.addstr(0, lh, ' {0} '.format(caption), curses.color_pair(self.COLOR_NORMAL) | curses.A_STANDOUT)
        if buttons:
            if len(buttons) > 1:
                bwid = 0
                for bcap in buttons:
                    if len(bcap) > bwid: bwid = len(bcap)
                cellwid = (width - 4) / len(buttons)
                lpad = (cellwid - bwid) / 2 - 1
                rpad = cellwid - bwid - lpad - 1
                self._dialogpad.move(height - 2, 1)
            else:
                bwid = len(buttons[0])
                lpad = rpad = 1
                self._dialogpad.move(height - 2, (width / 2) - (bwid / 2) - 2)
            for button in buttons:
                self._dialogpad.addstr('{0:{wlpad}}<{1:^{wbwid}}>{0:{wrpad}}'.format('',button, wlpad=lpad, wbwid=bwid, wrpad=rpad))
        dt = (self._screensize[0] / 2) - (height / 2)
        dl = (self._screensize[1] / 2) - (width / 2)
        dc = padcoords(sminrow=dt,smincol=dl,smaxrow=dt+height - 1, smaxcol=dl+width - 1)
        self._dialogcoords = dc
        self._dialogpad.overlay(self._screen, 0, 0, dc.sminrow, dc.smincol, dc.smaxrow, dc.smaxcol)
        self._screen.refresh()

    def _clearDialog(self):
        del self._dialogpad
        self._dialogpad = None
        self._dialogcoords = None
        self._screen.touchwin()
        self._screen.refresh()

    def _updateDialog(self):
        if self._dialogpad:
            self._screen.refresh()
            dc = self._dialogcoords
            self._dialogpad.refresh(0,0,dc.sminrow, dc.smincol, dc.smaxrow, dc.smaxcol)

    def _addDialogText(self, row, text, align='^'):
        self._dialogpad.addstr(row, 1, '{0:{aln}{wid}}'.format(text, aln=align, wid=self._dialogpad.getmaxyx()[1] - 2))
        self._updateDialog()

    def _addDialogProgress(self, row, current, total, showPercent=True, width=None):
        dc = self._dialogcoords
        if width is None:
            width = (dc.smaxcol - dc.smincol) * 2 / 3
        pct = float(current) / float(total)
        filled = int(pct * float(width))
        lh = ((dc.smaxcol - dc.smincol) / 2) - (width / 2)
        self._dialogpad.addch(row, lh - 1, '[', curses.color_pair(self.COLOR_NORMAL) | curses.A_BOLD)
        self._dialogpad.addch(row, lh + width, ']', curses.color_pair(self.COLOR_NORMAL) | curses.A_BOLD)
        self._dialogpad.addstr(row, lh, ' '*width, curses.color_pair(self.COLOR_NORMAL))
        self._dialogpad.addstr(row, lh, '|'*filled, curses.color_pair(self.COLOR_OK) | curses.A_BOLD)
        if showPercent:
            pctstr = '{0:4.0%}'.format(pct)
            lh = ((dc.smaxcol - dc.smincol) / 2) - (len(pctstr) / 2)
            self._dialogpad.addstr(row, lh, pctstr, curses.color_pair(self.COLOR_NORMAL) | curses.A_BOLD)
        self._updateDialog()

    def _checkInterface(self):
        dispatcher.connect(self._notifyDriverReady, ZWaveWrapper.SIGNAL_DRIVER_READY)
        dispatcher.connect(self._notifySystemReady, ZWaveWrapper.SIGNAL_SYSTEM_READY)
        dispatcher.connect(self._notifyNodeReady, ZWaveWrapper.SIGNAL_NODE_READY)
        dispatcher.connect(self._notifyValueChanged, ZWaveWrapper.SIGNAL_VALUE_CHANGED)
        dispatcher.connect(self._notifyNodeAdded, ZWaveWrapper.SIGNAL_NODE_ADDED)
        self._initDialog(10,60,['Cancel'],'Progress')
        self._addDialogText(2,'Initializing OpenZWave')
        self._log.info('Initializing OpenZWave via wrapper')
        self._wrapper = ZWaveWrapper(device=self._config['device'], config=self._config['config'], log=None)
        self._setTimer('initCheck', 3, self._checkIfInitialized)

        while not self._stop.isSet() and not self._wrapper.initialized:
            time.sleep(0.1)
            # TODO: handle keys here...

    def _runLoop(self):
        while not self._stop.isSet():   
            key = self._screen.getch()
            if key == curses.KEY_DOWN: self._switchItem(1)
            elif key == curses.KEY_UP: self._switchItem(-1)
            elif key == curses.KEY_LEFT: self._switchTab(-1)
            elif key == curses.KEY_RIGHT: self._switchTab(1)
            elif key == 0x09: self._nextMode()
            elif key is not None: self._handleMnemonic(key)

    def _handleMnemonic(self, key):
        for mnemonic, func in self._keys.iteritems():
            if key == ord(mnemonic[0].lower()) or key == ord(mnemonic[0].upper()):
                funcname = '_handle%s' % func
                try:
                    method = getattr(self, funcname)
                    method()
                except AttributeError:
                    msg = 'No method named %s defined!' % funcname
                    self._log.warn('handleMnemonic: %s', msg)
                    self._alert(msg)
                break

    def _switchItem(self, delta):
        if self._listMode:
            n = self._listindex + delta
            if n in range(0, self._listcount):
                self._listindex = n
                self._updateDeviceList() # TODO: we don't really need to redraw everything when selection changes
                self._updateDeviceDetail()

    def _switchTab(self, delta):
        if self._listMode:
            i = self._colheaders.index(self._sortcolumn)
            i += delta
            if i > len(self._colheaders) - 1: i = 1
            elif i < 1: i = len(self._colheaders) - 1
            self._sortcolumn = self._colheaders[i]
        else:
            i = self._detailheaders.index(self._detailview)
            i += delta
            if i > len(self._detailheaders) - 1: i = 0
            elif i < 0: i = len(self._detailheaders) - 1
            self._detailview = self._detailheaders[i]
        self._updateColumnHeaders()
        self._updateDeviceList()
        self._updateDeviceDetail()

    def _nextMode(self):
        self._listMode = not self._listMode
        self._updateColumnHeaders()

    def _shutdown(self):
        pass

    def _rightPrint(self, row, data, attrs=None):
        if attrs is None:
            attrs = curses.color_pair(self.COLOR_NORMAL)
        self._screen.addstr(row, self._screensize[1] - len(data), data, attrs)

    def _updateSystemInfo(self):
        self._screen.addstr(0,1,'{0} on {1}'.format(self._wrapper.controllerDescription, self._config['device']), curses.color_pair(self.COLOR_NORMAL))
        self._screen.addstr(1,1,'Home ID 0x%0.8x' % self._wrapper.homeId, curses.color_pair(self.COLOR_NORMAL))
        self._screen.move(2,1)
        self._screen.addstr('{0} Registered Nodes'.format(self._wrapper.nodeCount), curses.color_pair(self.COLOR_NORMAL))
        if self._wrapper.initialized:
            sleepcount = self._wrapper.sleepingNodeCount
            if sleepcount:
                self._screen.addstr(' ({0} Sleeping)'.format(sleepcount),curses.color_pair(self.COLOR_NORMAL) | curses.A_DIM)
        self._rightPrint(0, '{0} Library'.format(self._wrapper.libraryTypeName))
        self._rightPrint(1, 'Version {0}'.format(self._wrapper.libraryVersion))
        self._screen.refresh()

    def _updateColumnHeaders(self):
        self._screen.move(4,0)
        for text, wid in zip(self._colheaders, self._colwidths):
            clr = curses.color_pair(self.COLOR_HEADER_NORMAL) if self._listMode else curses.color_pair(self.COLOR_NORMAL) | curses.A_STANDOUT
            if text == self._sortcolumn:
                clr = curses.color_pair(self.COLOR_HEADER_HI) | curses.A_BOLD
            self._screen.addstr('{0:<{width}}'.format(text, width=wid), clr)

        self._screen.move(self._rowheights[0] + self._rowheights[1] + 1, 0)
        clr = curses.color_pair(self.COLOR_HEADER_NORMAL) if not self._listMode else curses.color_pair(self.COLOR_NORMAL) | curses.A_STANDOUT
        self._screen.addstr('{0:{width}}'.format('', width=self._screensize[1]), clr)
        self._screen.move(self._rowheights[0] + self._rowheights[1] + 1, 0)
        for text in self._detailheaders:
            clr = curses.color_pair(self.COLOR_HEADER_NORMAL) if not self._listMode else curses.color_pair(self.COLOR_NORMAL) | curses.A_STANDOUT
            if text == self._detailview:
                clr = curses.color_pair(self.COLOR_HEADER_HI) | curses.A_BOLD
            wid = len(text)
            self._screen.addstr(' {0:<{width}} '.format(text, width=wid), clr)

    def _updateDeviceList(self):
        # TODO: handle column sorting in list view
        self._listcount = self._wrapper.nodeCount
        #self._listpad.clear()
        idx = 0
        for node in self._wrapper._nodes.itervalues():
            clr = curses.color_pair(self.COLOR_NORMAL) | curses.A_STANDOUT if idx == self._listindex else curses.color_pair(self.COLOR_NORMAL)
            # self._colheaders=['','ID','Name','Location','Type','State','Batt','Signal']
            # TODO: add remaining columns
            # TODO: add highlight
            # TODO: for each column, ensure contents fit within width constraints
            self._listpad.addstr(idx,0,' {0:<{w1}}{1:<{w2}}{2:<{w3}}{3:<{w4}}'.format(node.id, node.name, node.location, node.productType, w1=self._colwidths[1], w2=self._colwidths[2], w3=self._colwidths[3], w4=self._colwidths[4]), clr)
            idx += 1

        ctop = self._rowheights[0]
        listheight = self._rowheights[1]
        if self._listindex - self._listtop > listheight:
            self._listtop = self._listindex - listheight
        elif self._listindex < self._listtop:
            self._listtop = self._listindex
        self._screen.refresh()
        self._listpad.refresh(self._listtop, 0, ctop, 0, ctop + listheight, self._screensize[1] - 1)
        self._updateDialog()

    def _updateDeviceDetail(self):
        pass

    def _updateMenu(self):
        menurow = self._screensize[0] - 1
        self._screen.addstr(menurow, 0, ' ' * (self._screensize[1] - 1), curses.color_pair(self.COLOR_HEADER_NORMAL))
        self._screen.move(menurow,4)
        for mnemonic, text in self._keys.iteritems():
            self._screen.addstr(' {0} '.format(mnemonic), curses.color_pair(self.COLOR_NORMAL) | curses.A_BOLD)
            self._screen.addstr('{0}'.format(text), curses.color_pair(self.COLOR_HEADER_NORMAL))

    def _redrawMenu(self):
        self._updateMenu()
        self._screen.refresh()

def main(stdscr):
    commander = ZWaveCommander(stdscr)
    commander.main()
    
curses.wrapper(main)

class DeleteMe:
    '''
              1         2         3         4         5         6         7         8
     12345678901234567890123456789012345678901234567890123456789012345678901234567890
    +--------------------------------------------------------------------------------+
    | HomeSeer Z-Troller on /dev/keyspan-2                         Installer Library | 1
    | Home ID 0x003d8522                                         Version Z-Wave 2.78 | 2
    | 7 Registered Nodes (2 Sleeping)                                                | 3
    |                                                                                | 4
    | ID  Name          Location      Type                    State    Batt   Signal | 5
    | 1   Controller                  Remote Controller       OK                     | 6    |
    | 2   Sconce 1      Living Room   Multilevel Switch       [||||  ]        [||||] | 7    |
    |>3   TV            Living Room   Binary Power Switch     on              [||| ] | 8    |
    | 4   Liv Rm Motion Living Room   Motion Sensor           sleeping [||||] [||||] | 9    |
    | 5   Sliding Door  Family Room   Door/Window Sensor      ALARM    [||| ] [||  ] | 10   +- Scrollable box, lists nodes
    | 6   Sconce 2      Living Room   Multilevel Switch       [||||  ]        [||||] | 11   |
    | 7   Bedroom Lamp  Master Bed    Multilevel Scene Switch on                     | 12   |
    |                                                                                | 13   |
    |                                                                                | 14   |
    | Name:         TV                      | Command Classes                        | 15
    | Location:     Living Room             | COMMAND_CLASS_BASIC                    | 16   |
    | Manufacturer: Aeon Labs               | COMMAND_CLASS_HAIL                     | 17   |
    | Product:      Smart Energy Switch     | COMMAND_CLASS_ASSOCIATION              | 18   |
    | Neighbors:    2,4,5,6,7               | COMMAND_CLASS_VERSION                  | 19   |
    | Version:      3                       | COMMAND_CLASS_SWITCH_ALL               | 20   |
    | State:        On                      | COMMAND_CLASS_MANUFACTURER_SPECIFIC    | 21   +- Scrollable box, toggles:
    | Signal:       3dbA (good)             | COMMAND_CLASS_CONFIGURATION            | 22   |  1) command classes
    |                                       | COMMAND_CLASS_SENSOR_MULTILEVEL        | 23   |  2) values
    |                                       | COMMAND_CLASS_METER                    | 24   |  3) groups
    |        Add Del Edit Refresh + - oN oFf Values Groups Classes Setup Quit        | 25   |  4) config params
    +---------------------------------------+----------------------------------------+

    [a]add          - associate new node
    [b]about        - show about dialog
    [c]classes      - view command classes
    [d]delete       - remove association
    [e]edit         (COMMAND_CLASS_CONFIGURATION or has editable values)
    [f]off          (command_class_switch_binary,command_class_switch_multilevel,COMMAND_CLASS_SWITCH_TOGGLE_BINARY,COMMAND_CLASS_SWITCH_TOGGLE_MULTILEVEL)
    [g]groups       (COMMAND_CLASS_ASSOCIATION)
    [n]on           (command_class_switch_binary,command_class_switch_multilevel,COMMAND_CLASS_SWITCH_TOGGLE_BINARY,COMMAND_CLASS_SWITCH_TOGGLE_MULTILEVEL)
    [r]refresh      - refresh specified node
    [s]setup
    [+]increase     (COMMAND_CLASS_SWITCH_MULTILEVEL)
    [-]decrease     (COMMAND_CLASS_SWITCH_MULTILEVEL)


    '''