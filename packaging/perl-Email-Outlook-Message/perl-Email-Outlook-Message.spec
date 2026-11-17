# perl-Email-Outlook-Message for EL9.
#
# Provides /usr/bin/msgconvert, which mail-archiver requires for .msg import
# and which no Rocky / EPEL / Fedora-EPEL repository carries for EL9.
# Built by build.sh in this directory (Rocky 9 container, EPEL + CRB, dnf only).
#
# Tests: the whole upstream suite runs in %%check, nothing is skipped
# (10 files, 122 tests). Every build and test dependency is packaged for
# EL9: Module::Build and Test::More in AppStream; IO::All, IO::String and
# Test::Pod::Coverage in CRB; the Email::* modules and OLE::Storage_Lite in
# EPEL. t/pod_coverage.t would skip itself without Test::Pod::Coverage, so
# that module is a BuildRequires. Nothing is fetched from CPAN.
#
# IO::All is used by the tests only (upstream lists it as a requirement
# "needed for testing"), so it is a BuildRequires and not a Requires.

Name:           perl-Email-Outlook-Message
Version:        0.921
Release:        1%{?dist}
Summary:        Read Outlook .msg files and convert them to MIME messages
License:        GPL-1.0-or-later OR Artistic-1.0-Perl
URL:            https://metacpan.org/release/Email-Outlook-Message
Source0:        https://www.cpan.org/authors/id/M/MV/MVZ/Email-Outlook-Message-%{version}.tar.gz
BuildArch:      noarch

# Build
BuildRequires:  coreutils
BuildRequires:  perl-generators
BuildRequires:  perl-interpreter
BuildRequires:  perl(Module::Build) >= 0.38
BuildRequires:  perl(strict)
BuildRequires:  perl(warnings)
# Run time
BuildRequires:  perl(base)
BuildRequires:  perl(Carp)
BuildRequires:  perl(Email::Address)
BuildRequires:  perl(Email::MIME) >= 1.923
BuildRequires:  perl(Email::MIME::ContentType) >= 1.014
BuildRequires:  perl(Email::MIME::Creator)
BuildRequires:  perl(Email::Sender) >= 1.3
BuildRequires:  perl(Email::Sender::Transport::Mbox)
BuildRequires:  perl(Email::Simple) >= 2.206
BuildRequires:  perl(Encode)
BuildRequires:  perl(File::Basename)
BuildRequires:  perl(Getopt::Long)
BuildRequires:  perl(IO::String)
BuildRequires:  perl(OLE::Storage_Lite) >= 0.14
BuildRequires:  perl(Pod::Usage)
BuildRequires:  perl(POSIX)
BuildRequires:  perl(vars)
# Tests
BuildRequires:  perl(Email::MIME::Modifier)
BuildRequires:  perl(IO::All)
BuildRequires:  perl(Test::More)
BuildRequires:  perl(Test::Pod::Coverage)
BuildRequires:  perl(utf8)

# The dependency generator finds every module the code uses, but without
# the minimum versions upstream declares in Build.PL. State those, and the
# distribution-level Email::Sender (only a sub-module is use'd).
Requires:       perl(Email::MIME) >= 1.923
Requires:       perl(Email::MIME::ContentType) >= 1.014
Requires:       perl(Email::Sender) >= 1.3
Requires:       perl(Email::Simple) >= 2.206
Requires:       perl(OLE::Storage_Lite) >= 0.14

# Drop the unversioned duplicates of the above.
%global __requires_exclude %{?__requires_exclude:%{__requires_exclude}|}^perl\\((Email::MIME|Email::MIME::ContentType|Email::Simple|OLE::Storage_Lite)\\)$

%description
Email::Outlook::Message reads e-mail messages stored as .msg files (such as
those written by Microsoft Outlook) and converts them to Email::MIME
objects. Outlook does not need to be installed.

The package also ships msgconvert, a command line tool that converts .msg
files to .eml files or appends them to an mbox.

%prep
%setup -q -n Email-Outlook-Message-%{version}

%build
perl Build.PL --installdirs=vendor
./Build

%install
./Build install --destdir=%{buildroot} --create_packlist=0
%{_fixperms} %{buildroot}/*

%check
./Build test

%files
# Upstream ships no separate licence file; the licence statement is the
# "COPYRIGHT AND LICENCE" section of README.
%license README
%doc CHANGELOG README TODO
%{_bindir}/msgconvert
%{perl_vendorlib}/Email/
%{_mandir}/man1/msgconvert.1*
%{_mandir}/man3/Email::Outlook::Message*.3pm*

%changelog
* Sat Nov 14 2026 Madhav Diwan <madhav@decllc.biz> - 0.921-1
- Initial EL9 package: mail-archiver requires /usr/bin/msgconvert for .msg
  import and no EL9 repository provides it
