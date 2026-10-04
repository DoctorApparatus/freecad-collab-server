# Loaded by FreeCAD at GUI startup. Note: __file__ is not defined here.
import FreeCAD as App
import FreeCADGui as Gui

try:
    import collab_sync.commands  # registers the Collab_* commands globally
except Exception as e:  # never break FreeCAD startup
    App.Console.PrintError(f"[Collab] failed to load: {e}\n")


class CollabWorkbench(Gui.Workbench):
    MenuText = "Collab"
    ToolTip = "Real-time collaboration on shared documents"
    Icon = """
/* XPM */
static char * collab_xpm[] = {
"16 16 3 1",
" 	c None",
".	c #3B82F6",
"+	c #F59E0B",
"                ",
"   ...     +++  ",
"  .....   +++++ ",
"  .....   +++++ ",
"  .....   +++++ ",
"   ...     +++  ",
"                ",
"  .......+++++  ",
" .........++++++",
" ........++++++ ",
" .......+++++++ ",
" .......+++++++ ",
" .......+++++++ ",
"                ",
"                ",
"                "};
"""

    def Initialize(self):
        from collab_sync import commands

        self.appendToolbar("Collab", commands.TOOLBAR)
        self.appendMenu("Collab", commands.MENU)

    def Activated(self):
        pass

    def Deactivated(self):
        pass

    def GetClassName(self):
        return "Gui::PythonWorkbench"


Gui.addWorkbench(CollabWorkbench())
