
import storytext.guishared, util, types, logging, sys, os
from storytext.definitions import UseCaseScriptError
from storytext.gridformatter import GridFormatter
# Some unused imports to make sure classloader loads necessary classes
from org.eclipse.swt.layout import GridLayout, FillLayout, FormLayout, RowLayout  #@UnusedImport
from org.eclipse import swt
from browserhtmlparser import BrowserHtmlParser
from java.util import Date
from java.io import File, FilenameFilter
from org.eclipse.jface.resource import ImageDescriptor
from array import array
from ordereddict import OrderedDict
from java.awt import Color
        
class ColorNameFinder:
    def __init__(self):
        self.names = {}
        self.widgetDefaults = set()
        # Add java.awt colors  
        self.addColors(Color, postfix="&")
        
    def shortenColorName(self, name, abbreviations):
        ret = name.lower()
        for text, repl in abbreviations:
            ret = ret.replace(text, repl)
        return ret

    def addColor(self, name, color, postfix="", modifiers=[], abbreviations=[]):
        if hasattr(color, "getRed"):
            newName = name + postfix
            nameToUse = self.shortenColorName(newName, abbreviations)
            self.names[self.getRGB(color)] = nameToUse
            for modifier, prefix in modifiers:
                rgb = self.getRGB(self.applyModifier(modifier, color))
                if rgb not in self.names:
                    self.names[rgb] = prefix + nameToUse
            
    def applyModifier(self, modifier, color):
        try:
            return modifier(color)
        except:
            colorToUse = swt.graphics.Color(swt.widgets.Display.getDefault(), color.getRed(), color.getGreen(), color.getBlue())
            return modifier(colorToUse)

    def addColors(self, cls, **kw):
        for name in sorted(cls.__dict__):
            if not name.startswith("__"):
                try:
                    color = getattr(cls, name)
                    self.addColor(name, color, **kw)
                except AttributeError:
                    pass
                    
    def addSWTColors(self, display):
        for name in sorted(swt.SWT.__dict__):
            if name.startswith("COLOR_"):
                colorKey = getattr(swt.SWT, name)
                color = display.getSystemColor(colorKey)
                rgb = self.getRGB(color)
                # Have to do this last because we can only retrieve them in the UI thread
                # Don't override any custom names we might have
                if rgb not in self.names:
                    self.names[rgb] = name[6:].lower()
                if "WIDGET" in name:
                    self.widgetDefaults.add(rgb)
                
    def getRGB(self, color):
        return color.getRed(), color.getGreen(), color.getBlue()

    def getName(self, color):
        return self.names.get(self.getRGB(color), "unknown")
    
    def getNameForWidget(self, color):
        rgb = self.getRGB(color)
        return self.names.get(rgb, "unknown") if rgb not in self.widgetDefaults else ""
        
        
colorNameFinder = ColorNameFinder()


class Describer(storytext.guishared.Describer):
    styleNames = [ (swt.widgets.CoolItem, []),
                   (swt.widgets.Item    , [ "SEPARATOR", "DROP_DOWN", "CHECK", "CASCADE", "RADIO" ]),
                   (swt.widgets.Button  , [ "CHECK", "RADIO", "TOGGLE", "ARROW", "UP", "DOWN" ]),
                   (swt.widgets.DateTime, [ "DATE", "TIME", "CALENDAR", "SHORT" ]),
                   (swt.widgets.Combo   , [ "READ_ONLY", "SIMPLE" ]),
                   (swt.custom.CCombo   , [ "READ_ONLY", "FLAT", "BORDER" ]), 
                   (swt.widgets.Text    , [ "PASSWORD", "SEARCH", "READ_ONLY" ]) ]
    ignoreWidgets = [ types.NoneType ]
    # DateTime children are an implementation detail
    # Coolbars, Toolbars and Expandbars describe their children directly : they have two parallel children structures
    ignoreChildren = (swt.widgets.CoolBar, swt.widgets.ExpandBar, swt.widgets.ToolBar, swt.widgets.DateTime, swt.widgets.Group)
    statelessWidgets = [ swt.widgets.Sash ]
    stateWidgets = [ swt.widgets.Shell, swt.widgets.Button, swt.widgets.Menu, swt.widgets.Link,
                     swt.widgets.CoolBar, swt.widgets.ToolBar, swt.widgets.Label, swt.custom.CLabel,
                     swt.widgets.Combo, swt.widgets.ExpandBar, swt.widgets.Text, swt.widgets.List,
                     swt.widgets.Tree, swt.widgets.DateTime, swt.widgets.TabFolder, swt.widgets.Table, 
                     swt.custom.CTabFolder, swt.widgets.Canvas, swt.browser.Browser, swt.custom.CCombo,
                     swt.widgets.Spinner, swt.widgets.Group, swt.widgets.Composite ]
    childrenMethodName = "getChildren"
    visibleMethodName = "getVisible"
    def __init__(self, canvasDescriberClasses=[]):
        storytext.guishared.Describer.__init__(self)
        self.canvasCounter = storytext.guishared.WidgetCounter()
        self.contextMenuCounter = storytext.guishared.WidgetCounter(self.contextMenusEqual)
        self.customTooltipCounter = storytext.guishared.WidgetCounter(self.tooltipsEqual)
        self.widgetsAppeared = []
        self.widgetsMoved = []
        self.parentsResized = set()
        self.widgetsDescribed = set()
        self.browserStates = {}
        self.clipboardText = None
        self.storedImages = {}
        self.imageToName = {}
        self.handleImages()
        self.screenshotNumber = 0
        self.colorsAdded = False
        self.canvasDescriberClasses = canvasDescriberClasses

    def setWidgetPainted(self, widget):
        if widget not in self.widgetsDescribed and widget not in self.windows and widget not in self.widgetsAppeared:
            self.logger.debug("Widget painted " + self.getRawData(widget))
            self.widgetsAppeared.append(widget)
        
    def setWidgetShown(self, widget):
        # Menu show events seem a bit spurious, they aren't really shown at this point:
        # ScrollBar shows are not relevant to anything
        if isinstance(widget, swt.widgets.Control) and widget not in self.widgetsAppeared:
            self.logger.debug("Widget shown " + self.getRawData(widget))
            self.widgetsAppeared.append(widget)
            if widget in self.widgetsMoved:
                self.widgetsMoved.remove(widget)
            
    def setWidgetMoved(self, widget):
        if isinstance(widget, swt.widgets.Control) and widget not in self.widgetsAppeared and widget.getParent() not in self.parentsResized:
            self.logger.debug("Widget moved " + self.getRawData(widget))
            self.widgetsMoved.append(widget)
        
    def setWidgetResized(self, widget):
        if isinstance(widget, swt.widgets.Control):
            self.parentsResized.add(widget)
            self.parentsResized.add(widget.getParent())
                    
    def addFilters(self, display):
        class ShowListener(swt.widgets.Listener):
            def handleEvent(listenerSelf, e):
                storytext.guishared.catchAll(self.setWidgetShown, e.widget)

        class PaintListener(swt.widgets.Listener):
            def handleEvent(listenerSelf, e):
                storytext.guishared.catchAll(self.setWidgetPainted, e.widget)

        class MoveListener(swt.widgets.Listener):
            def handleEvent(listenerSelf, e):
                storytext.guishared.catchAll(self.setWidgetMoved, e.widget)

        class ResizeListener(swt.widgets.Listener):
            def handleEvent(listenerSelf, e):
                storytext.guishared.catchAll(self.setWidgetResized, e.widget)

        display.addFilter(swt.SWT.Show, ShowListener())
        display.addFilter(swt.SWT.Paint, PaintListener())
        display.addFilter(swt.SWT.Move, MoveListener())
        display.addFilter(swt.SWT.Resize, ResizeListener())
        display.addFilter(swt.SWT.Dispose, ResizeListener()) # Being disposed is the ultimate resize :)

    def getScreenshotFileName(self, screenshotDir):
        return os.path.join(screenshotDir, "screenshot" + str(self.screenshotNumber) + ".png")

    def writeScreenshot(self, shell):
        display = shell.getDisplay()
        gc = swt.graphics.GC(display);
        image = swt.graphics.Image(display, shell.getBounds())
        gc.copyArea(image, shell.getBounds().x, shell.getBounds().y)
        gc.dispose()
        
        imageLoader = swt.graphics.ImageLoader()
        imageLoader.data = [ image.getImageData() ]
        self.screenshotNumber += 1
        screenshotDir = os.path.join(os.getenv("TEXTTEST_LOG_DIR", os.getcwd()), "screenshots")
        if not os.path.isdir(screenshotDir):
            os.makedirs(screenshotDir)
        fileName = self.getScreenshotFileName(screenshotDir)
        while os.path.isfile(fileName):
            self.screenshotNumber += 1
            fileName = self.getScreenshotFileName(screenshotDir)
        imageLoader.save(fileName, swt.SWT.IMAGE_PNG) 
    
    def describeWithUpdates(self, shellMethod):
        shell = shellMethod()
        if self.writeScreenshots:
            self.writeScreenshot(shell)
        if not self.colorsAdded:
            self.colorsAdded = True
            colorNameFinder.addSWTColors(shell.getDisplay())

        if shell in self.windows:
            stateChanges = self.findStateChanges(shell)
            stateChangeWidgets = [ widget for widget, _, _ in stateChanges ]
            if self.structureLog.isEnabledFor(logging.DEBUG):
                for widget in stateChangeWidgets:
                    self.structureLog.info("Widget changed state:")
                    self.describeStructure(widget)
            self.processMovedWidgets()
            describedForAppearance = self.describeAppearedWidgets(stateChangeWidgets, shell)
            self.describeStateChanges(stateChanges, describedForAppearance)
            
        self.widgetsAppeared = filter(lambda w: not w.isDisposed() and self.inDifferentShell(w, shell), self.widgetsAppeared)
        self.parentsResized = set()
        self.widgetsMoved = []
            
        if shell is not None:
            self.describeClipboardChanges(shell.getDisplay())
            self.describe(shell)
            
    def processMovedWidgets(self):
        # We are looking for cases of reordering: at least two widgets in the same parent must have moved for this to happen
        moved = filter(lambda w: not w.isDisposed() and self.describeClass(w.__class__.__name__), self.widgetsMoved)
        if len(moved) > 1:
            self.logger.debug("Handling moved widgets " + repr(map(self.getRawData, moved)))
            parents = [ w.getParent() for w in moved ]
            self.logger.debug("Parents " + repr(map(self.getRawData, parents)))
            for widget in moved:
                if parents.count(widget.getParent()) > 1:
                    self.widgetsAppeared.append(widget)
        
    def shouldCheckForUpdates(self, widget, shell):
        return not widget.isDisposed() and widget.getShell() == shell
    
    def inDifferentShell(self, widget, shell):
        return not isinstance(widget, swt.widgets.Shell) and widget.getShell() != shell
    
    def validAndShowing(self, widget):
        return not widget.isDisposed() and util.isVisible(widget) 

    def widgetShowing(self, widget, shell):
        return self.validAndShowing(widget) and not self.inDifferentShell(widget, shell)

    def describeClipboardChanges(self, display):
        clipboard = swt.dnd.Clipboard(display)
        textTransfer = swt.dnd.TextTransfer.getInstance()
        if self.clipboardText is None:
            # Initially. For some reason it doesn't let us set empty strings here
            # clearContents seemed the way to go, but seems not to work on Windows
            self.clipboardText = "dummy text for StoryText tests"
            clipboard.setContents([ self.clipboardText ], [ textTransfer ])
        else:
            newText = clipboard.getContents(textTransfer) or ""
            if newText != self.clipboardText:
                self.logger.info("Copied following to clipboard :\n" + newText)
                self.clipboardText = newText
        clipboard.dispose()
        
    def getWindowClasses(self):
        return swt.widgets.Shell, swt.widgets.Dialog

    def getTextEntryClass(self):
        return swt.widgets.Text

    def getWindowString(self):
        return "Shell"

    def getShellState(self, shell):
        return shell.getText()

    def getAllItemDescriptions(self, itemBar, indent=0, subItemMethod=None,
                               prefix="", selection=[], columnCount=0, enclosingJfaceTooltip=None, **kw):
        descs = []
        for item in itemBar.getItems():
            currPrefix = prefix + " " * indent * 2
            selected = item in selection or (hasattr(item, "getSelection") and item.getSelection())
            if columnCount:
                row = [ self.getItemColumnDescription(item, i, currPrefix, selected, enclosingJfaceTooltip) for i in range(columnCount) ]
                descs.append(row)
            else:
                itemDesc = self.getItemDescription(item, currPrefix, selected, enclosingJfaceTooltip)
                if itemDesc:
                    descs.append(itemDesc)
            if subItemMethod:
                descs += subItemMethod(item, indent, prefix=prefix, selection=selection, 
                                       columnCount=columnCount, enclosingJfaceTooltip=enclosingJfaceTooltip, **kw)
        return descs

    def getCascadeMenuDescriptions(self, item, indent, storeStatesForSubMenus=False, describeMenus=None, **kw):
        cascadeMenu = item.getMenu()
        if cascadeMenu:
            if describeMenus:
                text = item.getText().replace("&", "")
                if text not in describeMenus:
                    return []
            descs = self.getAllItemDescriptions(cascadeMenu, indent+1, subItemMethod=self.getCascadeMenuDescriptions, 
                                                storeStatesForSubMenus=storeStatesForSubMenus, **kw)
            if indent == 1 and storeStatesForSubMenus:
                self.widgetsWithState[cascadeMenu] = "\n".join(descs)
            return descs
        else:
            return []

    def getSubTreeDescriptions(self, item, indent, **kw):
        if item.getExpanded():
            return self.getAllItemDescriptions(item, indent+1, subItemMethod=self.getSubTreeDescriptions, **kw)
        else:
            return []

    def getExpandItemDescriptions(self, item, indent, *args, **kw):
        if item.getExpanded():
            return self.getCoolItemDescriptions(item, indent + 1)
        else:
            return []
        
    def getToolItemControls(self, item, indent, **kw):
        control = item.getControl()
        if control:
            return [ (control, indent) ] 
        else:
            return []

    def getCoolItemDescriptions(self, item, *args, **kw):
        return [ self.getItemControlDescription(c, i) for c, i in self.getToolItemControls(item, *args, **kw) ]

    def getItemControlDescription(self, control, indent):
        descLines = self.getDescription(control).splitlines()
        paddedLines = [ " " * indent * 2 + line for line in descLines ]
        return "\n".join(paddedLines) + "\n"
        
    def getMenuDescription(self, menu, indent=1, **kw):
        return self.getItemBarDescription(menu, indent=indent, subItemMethod=self.getCascadeMenuDescriptions, **kw)
    
    def getMenuState(self, menu):
        return self.getMenuDescription(menu, indent=2)
    
    def getMenuBarDescription(self, menubar):
        if menubar and self.describeClass("Menu"):
            describeMenus = self.excludeClassNames.get("Menu")
            return "Menu Bar:\n" + self.getMenuDescription(menubar, storeStatesForSubMenus=True, describeMenus=describeMenus)
        else:
            return ""

    def getExpandBarDescription(self, widget):
        state = self.getState(widget)
        self.widgetsWithState[widget] = state
        return "Expand Bar:\n" + self.getItemBarDescription(widget, indent=1, subItemMethod=self.getExpandItemDescriptions) 

    def getExpandBarState(self, expandbar):
        return expandbar.getChildren(), [ item.getExpanded() for item in expandbar.getItems() ] 

    def itemStateToString(self, itemState):
        if isinstance(itemState, (str, unicode)):
            return itemState
        else:
            return self.getItemControlDescription(*itemState)

    def getToolBarDescription(self, toolbar):
        itemStates = self.getToolBarState(toolbar)
        self.widgetsWithState[toolbar] = itemStates
        descs = map(self.itemStateToString, itemStates)
        return "\n".join(descs)

    def getToolBarState(self, toolbar):
        return [ "Tool Bar:" ] + self.getAllItemDescriptions(toolbar, indent=1, 
                                                             subItemMethod=self.getToolItemControls)
    
    def getCoolBarDescription(self, coolbar):
        state = self.getCoolBarState(coolbar)
        self.widgetsWithState[coolbar] = state
        desc = "Cool Bar"
        if state:
            desc += " (" + state + ") "
        return desc + ":\n" + self.getItemBarDescription(coolbar, indent=1, subItemMethod=self.getCoolItemDescriptions)

    def getCoolBarState(self, coolbar):
        return colorNameFinder.getNameForWidget(coolbar.getBackground())

    def contextMenusEqual(self, menu1, menu2):
        return [ (item.getText(), item.getEnabled()) for item in menu1.getItems() ] == \
               [ (item.getText(), item.getEnabled()) for item in menu2.getItems() ]

    def imagesEqual(self, image1, image2):
        return image1.getImageData().data == image2.getImageData().data

    def tooltipsEqual(self, data1, data2):
        tip1, widget1 = data1
        tip2, widget2 = data2
        return tip1 == tip2 and widget1 == widget2

    def getImageDescription(self, image):
        # Seems difficult to get any sensible image information out, there is
        # basically no query API for this in SWT
        if self.imageDescriptionType == "name":
            return self.getImageNameDescription(image)
        else:
            return self.getDefaultImageDescription(image)
        
    def getImageNameDescription(self, image):
        desc = self.getImageName(image)
        if desc is not None:
            return "Icon '" + desc + "'"
        else:
            return "Unknown Image"

    def getDefaultImageDescription(self, image):
        name = "Image"
        if self.imageDescriptionType == "number":
            name += " " + self.imageCounter.getId(image)
        return name
    
    def getPixels(self, data):
        pixels = array('i', (0, ) * data.width * data.height)
        data.getPixels(0, 0, data.width * data.height, pixels, 0)
        return pixels

    def imageDataMatches(self, data, data2, hasExcessData):
        if hasExcessData:
            return self.getPixels(data) == self.getPixels(data2)
        else:
            return data.data == data2.data

    def getImageName(self, image):
        name = self.imageToName.get(image)
        if name is not None:
            return name

        data = image.getImageData()
        hasExcessData = data.width * data.depth / 8 < data.bytesPerLine
        imageDict = self.storedImages.get((data.width, data.height), {})
        for name, imgData in imageDict.items():
            if self.imageDataMatches(data, imgData, hasExcessData):
                baseName = os.path.basename(name)
                self.imageToName[image] = baseName             
                return baseName
       
    def storeImageData(self, url):
        imgDesc = ImageDescriptor.createFromURL(url)
        name = url.getFile()
        self.logger.debug("Storing image data for file " + name)
        if imgDesc is not None:
            newImage = imgDesc.createImage()
            data = newImage.getImageData()
            imageDict = self.storedImages.setdefault((data.width, data.height), OrderedDict())
            if name not in imageDict:
                imageDict[name] = data
            newImage.dispose()

    def getCanvasDescription(self, widget):
        return self.getAndStoreState(widget)
    
    def getCanvasState(self, widget):
        for canvasDescriberClass in self.canvasDescriberClasses:
            if canvasDescriberClass.canDescribe(widget):
                return canvasDescriberClass(widget).getCanvasDescription(self)
        return "Canvas " + self.canvasCounter.getId(widget)

    def findStyleList(self, item):
        for widgetClass, styleList in self.styleNames:
            if isinstance(item, widgetClass):
                return styleList
        return []

    def getStyleDescriptions(self, item):
        styleList = self.findStyleList(item)
        style = item.getStyle()
        descs = []
        for tryStyle in styleList:
            if style & getattr(swt.SWT, tryStyle) != 0:
                descs.append(tryStyle.lower().replace("_", " ").replace("separator", "---"))
        return descs

    def getItemColumnDescription(self, item, colIndex, prefix, *args):
        elements = [ item.getText(colIndex) ]
        if colIndex:
            if item.getImage(colIndex):
                elements.append(self.getImageDescription(item.getImage(colIndex)))
        else:
            elements += self.getPropertyElements(item, *args)
        
        desc = self.combineElements(elements)
        if desc and colIndex == 0:
            return prefix + desc
        else:
            return desc
        
    def hasPrivateMethod(self, obj, methodName):
        return any((method.getName() == methodName for method in obj.getClass().getDeclaredMethods())) 
        
    def getJfaceTooltip(self, item):
        for listener in item.getListeners(swt.SWT.MouseHover):
            tooltip = self.getEnclosingInstance(listener)
            if self.hasPrivateMethod(tooltip, "createToolTipContentArea"):
                return tooltip

    def getControlDecorations(self, item):
        decorations = []
        for listener in self.getControlDecorationListeners(item):
            deco = self.getEnclosingInstance(listener)
            if deco:
                decorations.append(deco)
        return decorations

    def getControlDecorationListeners(self, item):
        listeners = []
        for typedListener in item.getListeners(swt.SWT.FocusIn):
            if hasattr(typedListener, "getEventListener"):
                focusListener = typedListener.getEventListener()
                if "ControlDecoration" in focusListener.__class__.__name__:
                    listeners.append(focusListener)
        return listeners

    def getEnclosingInstance(self, listener):
        cls = listener.getClass()
        for field in cls.getDeclaredFields():
            if field.getName().startswith("this"):
                field.setAccessible(True)
                return field.get(listener)
       
    def getControlDecorationDescription(self, item):
        for deco in self.getControlDecorations(item):
            if deco:
                image = deco.getImage()
                imgDesc = self.getImageDescription(deco.getImage()) if image is not None else ""
            if deco and self.decorationVisible(deco): 
                text = "Decoration " + imgDesc
                desc = deco.getDescriptionText()
                if desc:
                    text += "\n'" + desc + "'"
                return text
            
    def decorationVisible(self, deco):
        if hasattr(deco, "isVisible"): # added in 3.6
            return deco.isVisible()
        else:
            # Workaround for reflection bug in Jython 2.5.1
            # args = (None,) if sys.version_info[:3] <= (2, 5, 1) else ()
            # Jython 2.5.2 doesn't work anyway so we don't include this fix for now
            return util.callPrivateMethod(deco, "shouldShowDecoration")

    def isCustomTooltip(self, jfaceTooltip):
        return not jfaceTooltip.__class__.__module__.startswith("org.eclipse.jface")

    def getToolTipText(self, item, jfaceTooltip):
        if hasattr(item, "getToolTipText") and item.getToolTipText():
            return item.getToolTipText()
        elif jfaceTooltip and not self.isCustomTooltip(jfaceTooltip):
            event = self.makeToolTipEvent(item)
            if util.callPrivateMethod(jfaceTooltip, "shouldCreateToolTip", [ event ]):
                return util.callPrivateMethod(jfaceTooltip, "getText", [ event ])
        
    def getPropertyElements(self, *args, **kw):
        return self.getPropertyElementsAndTooltip(*args, **kw)[0]
        
    def getPropertyElementsAndTooltip(self, item, selected=False, enclosingJfaceTooltip=None):
        elements = []
        decoText = self.getControlDecorationDescription(item)
        if decoText:
            elements.append(decoText)
        if isinstance(item, swt.widgets.Spinner):
            elements += self.getSpinnerPropertyElements(item)
            
        jfaceTooltip = enclosingJfaceTooltip if isinstance(item, swt.widgets.Item) else self.getJfaceTooltip(item)
        tooltipText = self.getToolTipText(item, jfaceTooltip)
        if tooltipText:
            elements.append("Tooltip '" + tooltipText + "'")
        elements += self.getStyleDescriptions(item)
        if hasattr(item, "getImage") and item.getImage():
            elements.append(self.getImageDescription(item.getImage()))
        if hasattr(item, "getEnabled") and not item.getEnabled():
            elements.append("greyed out")
        if selected:
            elements.append("selected")
        elements.append(self.getContextMenuReference(item))
        customTooltipText = self.getCustomTooltipReference(item, jfaceTooltip)
        if customTooltipText:
            elements.append(customTooltipText)
        if hasattr(item, "getItemCount") and hasattr(item, "getExpanded") and item.getItemCount() > 0 and not item.getExpanded():
            elements.append("+")
        return elements, jfaceTooltip

    def getSpinnerPropertyElements(self, item):
        elements = []
        min = item.getMinimum()
        if min != 0:
            elements.append("Min " + str(min))
        elements.append("Max " + str(item.getMaximum()))
        step = item.getIncrement()
        if step != 1:
            elements.append("Step " + str(step))
        step = item.getPageIncrement()
        if step != 10:
            elements.append("Page Step " + str(step))
        return elements

    def getLabelState(self, label):
        if label.getStyle() & swt.SWT.SEPARATOR:
            if label.getStyle() & swt.SWT.VERTICAL:
                return "-" * 5 + "vertical" + "-" * 5
            else:
                return "-" * 10
        elements = []
        if label.getText():
            elements.append("'" + label.getText() + "'")
        for fontData in label.getFont().getFontData():
            fontStyle = fontData.getStyle()
            for fontAttr in [ "BOLD", "ITALIC" ]:
                if fontStyle & getattr(swt.SWT, fontAttr):
                    elements.append(fontAttr.lower())
        if label.getImage():
            elements.append(self.getImageDescription(label.getImage()))
        elements.append(self.getContextMenuReference(label))
        return self.combineElements(elements)

    def getLabelDescription(self, label):
        return self.getAndStoreState(label)
    
    getCLabelDescription = getLabelDescription
    getCLabelState = getLabelState

    def getButtonDescription(self, widget):
        desc = "Button"
        if widget.getText():
            desc += " '" + widget.getText() + "'"
        properties = self.getButtonState(widget)
        self.widgetsWithState[widget] = properties
        elements = [ desc ] + properties 
        return self.combineElements(elements)

    def getButtonState(self, widget):
        return self.getPropertyElements(widget, selected=widget.getSelection())

    def getSashDescription(self, widget):
        orientation = "Horizontal"
        if widget.getStyle() & swt.SWT.VERTICAL:
            orientation = "Vertical"
        return "-" * 15 + " " + orientation + " sash " + "-" * 15

    def getLinkDescription(self, widget):
        return self.getAndStoreState(widget)

    def getLinkState(self, widget):
        return "Link '" + widget.getText() + "'"

    def getBrowserDescription(self, widget):
        state = self.getBrowserState(widget)
        self.widgetsWithState[widget] = state
        return self.addHeaderAndFooter(widget, state)

    def getBrowserState(self, widget):
        url = util.getRealUrl(widget)
        if url and not url.startswith("file://"):
            return url
        # Ignore non-breaking spaces, they are invisible anyway
        # Webkit returns them in invalid format without the semicolons... handle that too.
        text = widget.getText().replace(u"\xa0", " ")
        return BrowserHtmlParser().parse(text)
        
    def getUpdatePrefix(self, widget, oldState, state):
        if isinstance(widget, (self.getTextEntryClass(), swt.browser.Browser, swt.widgets.Spinner)):
            return "\nUpdated " + (util.getTextLabel(widget, useContext=True) or self.getShortWidgetIdentifier(widget) or "Text") +  " Field\n"
        elif isinstance(widget, (swt.widgets.Combo, swt.custom.CCombo)):
            return "\nUpdated " + util.getTextLabel(widget, useContext=True) + " Combo Box\n"
        elif util.getTopControl(widget) or isinstance(widget, swt.widgets.Group):
            return "\n"
        elif isinstance(widget, swt.widgets.Menu):
            parentItem = widget.getParentItem()
            menuRefNr = self.contextMenuCounter.getWidgetNumber(widget)
            menuRefNr = " " + str(menuRefNr) if menuRefNr > 0 else ""
            menuName = parentItem.getText() if parentItem else "Context"
            return "\nUpdated " + menuName + " Menu" + menuRefNr +":\n"
        elif isinstance(widget, (swt.widgets.Label, swt.custom.CLabel)) and len(state) == 0:
            return "\nLabel now empty, previously " + oldState
        elif isinstance(widget, swt.widgets.Canvas) and not isinstance(widget, swt.custom.CLabel):
            for canvasDescriberClass in self.canvasDescriberClasses:
                if canvasDescriberClass.canDescribe(widget):
                    return canvasDescriberClass(widget).getUpdatePrefix(oldState, state)
        
        return "\nUpdated "

    def getShortWidgetIdentifier(self, widget):
        return widget.getData("org.eclipse.swtbot.widget.key")

    def getState(self, widget):
        if widget.isDisposed():
            # Will be caught, and the widget cleaned up
            raise UseCaseScriptError, "Widget is Disposed"
        else:
            return self.getSpecificState(widget)

    def getTextState(self, widget):
        return widget.getText(), self.getPropertyElements(widget)

    def getComboState(self, widget):
        return self.getTextState(widget)

    def getTextDescription(self, widget):
        contents, properties = self.getState(widget)
        self.widgetsWithState[widget] = contents, properties
        desc = self.addHeaderAndFooter(widget, contents)
        return self.combineElements([ desc ] + properties)

    def getComboDescription(self, widget):
        return self.getTextDescription(widget)

    getCComboDescription = getComboDescription
    getCComboState = getComboState

    def getTreeDescription(self, widget):
        return self.getAndStoreState(widget)

    def getTableDescription(self, widget):
        return self.getAndStoreState(widget)

    def getListDescription(self, widget):
        return self.getAndStoreState(widget)

    def getDateTimeDescription(self, widget):
        return self.getAndStoreState(widget)
    
    def getSpinnerDescription(self, widget):
        return self.getTextDescription(widget)
    
    def getSpinnerState(self, widget):
        return self.getTextState(widget)

    def getDateString(self, widget):
        if widget.getStyle() & swt.SWT.TIME:
            widgetDate = Date()
            widgetDate.setHours(widget.getHours())
            widgetDate.setMinutes(widget.getMinutes())
            widgetDate.setSeconds(widget.getSeconds())
            return util.getDateFormat(swt.SWT.TIME).format(widgetDate)
        else:
            widgetDate = Date(widget.getYear() - 1900, widget.getMonth(), widget.getDay())
            return util.getDateFormat(swt.SWT.DATE).format(widgetDate)

    def getDateTimeState(self, widget):
        elements = [ "DateTime" ] + self.getPropertyElements(widget) + [ "showing " + self.getDateString(widget) ]
        return self.combineElements(elements)

    def getListState(self, widget):
        text = self.combineElements([ "List" ] + self.getPropertyElements(widget)) + " :\n"
        selection = widget.getSelection()
        for item in widget.getItems():
            text += "-> " + item
            if item in selection:
                text += " (selected)"
            text += "\n"
        return text

    def getContextMenuReference(self, widget):
        if "Menu" not in self.excludeClassNames and not isinstance(widget, swt.widgets.MenuItem) and hasattr(widget, "getMenu") and widget.getMenu():
            return "Context Menu " + self.contextMenuCounter.getId(widget.getMenu())
        else:
            return ""
        
    def getCustomTooltipReference(self, item, jfaceTooltip):
        if "CustomTooltip" not in self.excludeClassNames and jfaceTooltip and self.isCustomTooltip(jfaceTooltip):
            itemTooltip = self.hasPrivateMethod(jfaceTooltip, "createViewerToolTipContentArea")
            isItem = isinstance(item, swt.widgets.Item)
            if isItem == itemTooltip:
                return "Custom Tooltip " + self.customTooltipCounter.getId((jfaceTooltip, item))

    def getTreeState(self, widget):
        columns = widget.getColumns()
        columnCount = len(columns)
        props, jfaceTooltip = self.getPropertyElementsAndTooltip(widget)
        text = self.combineElements([ "Tree" ] + props) + " :\n"
        rows = self.getAllItemDescriptions(widget, indent=0, subItemMethod=self.getSubTreeDescriptions,
                                           prefix="-> ", selection=widget.getSelection(),
                                           columnCount=columnCount, enclosingJfaceTooltip=jfaceTooltip)
        if columnCount > 0:
            rows.insert(0, [ c.getText() for c in columns ])
            text += str(GridFormatter(rows, columnCount))
        else:
            text += "\n".join(rows)
        return text

    def getTableState(self, widget):
        columns = widget.getColumns()
        columnCount = len(columns)
        props, jfaceTooltip = self.getPropertyElementsAndTooltip(widget)
        text = self.combineElements([ "Table" ] + props) + " :\n"
        rows = self.getAllTableItemDescriptions(widget, indent=0, 
                                                selection=widget.getSelection(),
                                                columnCount=columnCount,
                                                enclosingJfaceTooltip=jfaceTooltip)
        sortColumn = widget.getSortColumn()
        if widget.getSortDirection() == swt.SWT.UP:
            sortDirection = "(->)"
        elif widget.getSortDirection() == swt.SWT.DOWN:
            sortDirection = "(<-)"
        else:
            sortDirection = ""
        headerRow = [ c.getText() + sortDirection  if c == sortColumn else c.getText() for c in columns if c.getWidth() > 0] # Don't show hidden columns
        return text + self.formatTable(headerRow, rows, max(1, columnCount))

    def getAllTableItemDescriptions(self, widget, indent=0,
                                    prefix="", selection=[], columnCount=0, enclosingJfaceTooltip=None):
        descs = []
        for item in widget.getItems():
            currPrefix = prefix + " " * indent * 2
            selected = item in selection
            if columnCount:
                row = [ self.getItemColumnDescription(item, i, currPrefix, selected, enclosingJfaceTooltip) 
                        for i in range(columnCount) if widget.getColumn(i).getWidth() > 0 ]
                descs.append(row)
            else:
                descs.append([ self.getItemDescription(item, currPrefix, selected, enclosingJfaceTooltip) ])
        return descs

    def getTabFolderDescription(self, widget):
        state = self.getState(widget)
        self.widgetsWithState[widget] = state
        if state:
            return "TabFolder with tabs " + state
        else:
            return "TabFolder with no tabs"

    def getTabFolderState(self, widget):
        return " , ".join(self.getAllItemDescriptions(widget, selection=widget.getSelection()))

    def getCTabFolderState(self, widget):
        return " , ".join(self.getAllItemDescriptions(widget, selection=[ widget.getSelection() ]))

    getCTabFolderDescription = getTabFolderDescription

    def getCompositeState(self, widget):
        return util.getTopControl(widget)

    def getCompositeDescription(self, widget):
        return self.combineElements([ self.getStateControlDescription(widget), self.getContextMenuReference(widget) ])
    
    def getGroupState(self, widget):
        return self.getCompositeState(widget), widget.getText()
    
    def getGroupDescription(self, widget):
        header = "." * 6 + " " + widget.getText() + " " + "." * 6
        footer = "." * len(header)
        compositeDesc = self.getCompositeDescription(widget) or self.formatChildrenDescription(widget)
        return header + "\n" + str(compositeDesc) + "\n" + footer

    def getStateControlDescription(self, widget):
        stateControlInfo = self.getState(widget)
        stateControl = stateControlInfo[0] if isinstance(stateControlInfo, tuple) else stateControlInfo
        if stateControlInfo:
            self.widgetsWithState[widget] = stateControlInfo
        return self.getDescription(stateControl) if stateControl else ""
        
    def getVerticalDividePositions(self, children):
        positions = []
        for child in children:
            if isinstance(child, swt.widgets.Sash) and child.getStyle() & swt.SWT.VERTICAL:
                positions.append(child.getLocation().x)
        return sorted(positions)

    def layoutSortsChildren(self, widget):
        layout = widget.getLayout()
        return layout is not None and isinstance(layout, (swt.layout.GridLayout, swt.layout.FillLayout,
                                                          swt.layout.RowLayout, swt.custom.StackLayout))
    
    def _getDescription(self, widget):
        self.widgetsDescribed.add(widget)
        desc = storytext.guishared.Describer._getDescription(self, widget)
        if desc and isinstance(widget, (swt.widgets.ExpandBar, swt.widgets.Tree, swt.widgets.List, swt.widgets.Table)):
            desc = unicode(desc) + self.formatContextMenuDescriptions()
        return desc
    
    def handleGridFormatter(self, formatter):
        output = storytext.guishared.Describer.handleGridFormatter(self, formatter)
        if isinstance(output, (str, unicode)):
            output += self.formatContextMenuDescriptions()
        return output

    def getWindowContentDescription(self, shell):
        desc = ""
        desc = self.addToDescription(desc, self.getMenuBarDescription(shell.getMenuBar()))
        desc = self.addToDescription(desc, self.getChildrenDescription(shell))
        desc += self.formatContextMenuDescriptions()
        return desc
    
    def shouldDescribeChildren(self, widget):
        # Composites with StackLayout use the topControl rather than the children
        return storytext.guishared.Describer.shouldDescribeChildren(self, widget) and not util.getTopControl(widget)

    def _getChildrenDescription(self, widget):
        if self.shouldDescribeChildren(widget):
            return self.formatChildrenDescription(widget)
        else:
            self.markDescendantsDescribed(widget)
            return ""

    def markDescendantsDescribed(self, widget):
        if hasattr(widget, self.childrenMethodName):
            self.logger.debug("Mark descendants for " + self.getRawData(widget))
            children = getattr(widget, self.childrenMethodName)()
            self.widgetsDescribed.update(children)
            for child in children:
                self.markDescendantsDescribed(child)
        
    def formatContextMenuDescriptions(self):
        text = ""
        if "Menu" not in self.excludeClassNames:
            for contextMenu, menuId in self.contextMenuCounter.getWidgetsForDescribe():
                if not contextMenu.isDisposed():
                    menuDesc = self.getMenuDescription(contextMenu)
                    text += "\n\nContext Menu " + str(menuId) + ":\n" + menuDesc
                    self.widgetsWithState[contextMenu] = self.getMenuState(contextMenu)
        if "CustomTooltip" not in self.excludeClassNames:
            for (tooltip, widget), tooltipId in self.customTooltipCounter.getWidgetsForDescribe():
                text += "\n\nCustom Tooltip " + str(tooltipId) + ":\n" + self.getCustomTooltipDescription(tooltip, widget)
        return text
    
    def makeToolTipEvent(self, widgetOrItem):
        event = swt.widgets.Event()
        event.type = swt.SWT.MouseHover
        if isinstance(widgetOrItem, swt.widgets.Item):
            event.widget = widgetOrItem.getParent()
            event.item = widgetOrItem
            bounds = widgetOrItem.getBounds()
            event.x = util.getInt(bounds.x) + util.getInt(bounds.width) / 2
            event.y = util.getInt(bounds.y) + util.getInt(bounds.height) / 2
        else:
            event.widget = widgetOrItem
            event.item = None
            event.x = -1
            event.y = -1
        return event

    def getCustomTooltipDescription(self, tooltip, widget):
        event = self.makeToolTipEvent(widget)
        if util.callPrivateMethod(tooltip, "shouldCreateToolTip", [ event ]):
            shell = swt.widgets.Shell()
            result = util.callPrivateMethod(tooltip, "createToolTipContentArea", [ event, shell ], [ swt.widgets.Event, swt.widgets.Composite ])
            desc = self.getDescription(result)
            result.dispose()
            shell.dispose()
            return desc
        else:
            return ""
        
    def getHorizontalSpan(self, widget, columns):
        layout = widget.getLayoutData()
        if hasattr(layout, "horizontalSpan"):
            return min(layout.horizontalSpan, columns)
        else:
            return 1

    def usesGrid(self, widget):
        return isinstance(widget.getLayout(), swt.layout.GridLayout)

    def getLayoutColumns(self, widget, childCount, sortedChildren):
        layout = widget.getLayout()
        if hasattr(layout, "numColumns"):
            return layout.numColumns
        elif hasattr(layout, "type"):
            if layout.type == swt.SWT.HORIZONTAL:
                return childCount
        elif isinstance(layout, swt.layout.FormLayout):
            currColumns, maxColumns = 1, 1
            for child in sortedChildren:
                layoutData = child.getLayoutData()
                if layoutData.right and layoutData.right.control:
                    currColumns += 1
                    if currColumns > maxColumns:
                        maxColumns = currColumns
                else:
                    currColumns = 1
            return maxColumns
        return 1

    def getRawDataLayoutDetails(self, layout, *args):
        return [ str(layout.numColumns) + " columns" ] if hasattr(layout, "numColumns") else []

    def handleImages(self):
        if self.imageDescriptionType:
            self.buildImages()
    
    def buildImages(self):
        self.buildImagesFromPaths()

    def buildImagesFromPaths(self):
        for path in self.imagePaths:
            self.findFiles(File(path))

    def getImageFiles(self, path):
        d = File(path)
        class Filter(FilenameFilter):
            def accept(lself, d, fileName):#@NoSelf
                return fileName.endswith(".gif") or fileName.endswith(".png") or fileName.endswith(".jpg")

        return d.listFiles(Filter())
    
    def findFiles(self, pathAsFile):
        if pathAsFile.isFile() and self.isImageType(pathAsFile.getName()):
            self.storeImageData(pathAsFile.toURI().toURL())
        elif pathAsFile.isDirectory():
            for f in pathAsFile.listFiles():
                if f is not None:
                    self.findFiles(f)

    def isImageType(self, fileName):
        return fileName.endswith(".gif") or fileName.endswith(".png") or fileName.endswith(".jpg")

    def checkWindow(self, window):
        # Don't describe tooltips
        return (window.getStyle() & swt.SWT.TOOL) == 0

    def splitState(self, state):
        return state if isinstance(state, list) else state.split("\n")

    def getStateChangeDescription(self, widget, oldState, state):
        if isinstance(widget, (swt.widgets.Menu, swt.widgets.ToolBar)):
            old = self.splitState(oldState)
            new = self.splitState(state)
            if len(old) == len(new):
                return self.getDiffedDescription(widget, old, new)
        return storytext.guishared.Describer.getStateChangeDescription(self, widget, oldState, state)
    
    def describeStructure(self, widget, indent=0, **kw):
        storytext.guishared.Describer.describeStructure(self, widget, indent, **kw)
        if isinstance(widget, swt.widgets.Canvas):
            for canvasDescriberClass in self.canvasDescriberClasses:
                if canvasDescriberClass.canDescribe(widget):
                    canvasDescriberClass(widget).describeCanvasStructure(indent+1)

