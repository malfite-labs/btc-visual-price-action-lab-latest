using System;
using System.Runtime.InteropServices;
using System.Windows.Forms;
using Microsoft.Web.WebView2.WinForms;
public static class KlineHost {
 [DllImport("user32.dll",SetLastError=true)] static extern IntPtr SetParent(IntPtr child,IntPtr parent);
 [DllImport("user32.dll",SetLastError=true)] static extern IntPtr GetParent(IntPtr h);
 [DllImport("user32.dll",SetLastError=true)] static extern int GetWindowLong(IntPtr h,int index);
 [DllImport("user32.dll",SetLastError=true)] static extern int SetWindowLong(IntPtr h,int index,int value);
 [DllImport("user32.dll")] static extern bool GetClientRect(IntPtr h,out RECT r);
 [DllImport("user32.dll")] static extern bool MoveWindow(IntPtr h,int x,int y,int w,int hgt,bool repaint);
 [DllImport("user32.dll")] static extern bool IsWindow(IntPtr h);
 [StructLayout(LayoutKind.Sequential)] struct RECT { public int L,T,R,B; }
 [STAThread] public static void Main(string[] args) {
  if(args.Length!=1)throw new ArgumentException("PARENT_REQUIRED");
  IntPtr parent=new IntPtr(long.Parse(args[0]));if(!IsWindow(parent))throw new ArgumentException("PARENT_INVALID");
  Application.EnableVisualStyles();
  Form f=new Form();f.Text="BTC Kline Host";f.FormBorderStyle=FormBorderStyle.None;f.ShowInTaskbar=false;
  WebView2 view=new WebView2();view.Dock=DockStyle.Fill;f.Controls.Add(view);
  IntPtr child=f.Handle;int style=GetWindowLong(child,-16);SetWindowLong(child,-16,(style & ~unchecked((int)0x80000000))|0x40000000);
  SetParent(child,parent);if(GetParent(child)!=parent)throw new InvalidOperationException("PARENT_MISMATCH");
  f.Shown += async delegate { try {
   await view.EnsureCoreWebView2Async();
   view.CoreWebView2.NewWindowRequested += delegate(object sender,Microsoft.Web.WebView2.Core.CoreWebView2NewWindowRequestedEventArgs e){e.Handled=true;};
   view.CoreWebView2.NavigationStarting += delegate(object sender,Microsoft.Web.WebView2.Core.CoreWebView2NavigationStartingEventArgs e){Uri u;if(!Uri.TryCreate(e.Uri,UriKind.Absolute,out u)||u.Scheme!="http"||u.Host!="127.0.0.1"||u.Port!=5175||u.AbsolutePath!="/kline")e.Cancel=true;};
   view.CoreWebView2.Navigate("http://127.0.0.1:5175/kline");
  } catch(Exception e) { Console.Error.WriteLine("WEBVIEW_INIT_ERROR:"+e.GetType().Name+":"+e.Message);f.Close(); } };
  Timer timer=new Timer();timer.Interval=100;timer.Tick += delegate {if(!IsWindow(parent)){f.Close();return;} RECT r;if(GetClientRect(parent,out r)&&(f.ClientSize.Width!=r.R-r.L||f.ClientSize.Height!=r.B-r.T))MoveWindow(child,0,0,r.R-r.L,r.B-r.T,true);};timer.Start();
  Application.Run(f);timer.Dispose();view.Dispose();
 }
}
